"""End-to-end coverage for scripts/post-maintenance-verify.sh.

The script runs against stub `kubectl` and `sleep` on PATH. The pure parsers it
sources are covered by test_maintenance_lib.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "post-maintenance-verify.sh"

STUB_KUBECTL = r'''#!/usr/bin/env python3
"""Stub kubectl: answers every query post-maintenance-verify.sh makes from a
JSON fixture named by $STUB_STATE."""
import json
import os
import sys

state = json.load(open(os.environ["STUB_STATE"]))
args = [a for a in sys.argv[1:] if not a.startswith("--request-timeout")]
verb = args[0] if args else ""


def jsonpath() -> str:
    for i, a in enumerate(args):
        if a == "-o" and i + 1 < len(args):
            return args[i + 1]
    return ""


def flag(name: str) -> str:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
    return ""


def emit(lines):
    sys.stdout.write("".join(f"{line}\n" for line in lines))


if verb in ("run", "delete"):
    sys.exit(0 if state.get("dns_pod_creates", True) else 1)

if verb == "logs":
    print("DNS_" + state.get("dns", "PASS"))
    sys.exit(0)

if verb != "get":
    sys.exit(0)

kind = args[1] if len(args) > 1 else ""

if kind == "nodes":
    if not state.get("nodes_query_ok", True):
        sys.exit(1)
    nodes = state["nodes"]
    if "--no-headers" in args:
        emit(f"{n['name']} {n['status']} <none> 30d v1.34.1+k3s1" for n in nodes)
    else:
        emit("{}\t{}\t{}".format(n["name"], n.get("annot", ""),
                                 "true" if n.get("cordoned") else "")
             for n in nodes)
    sys.exit(0)

if kind == "pod":
    print(state.get("dns_phase", "Succeeded"))
    sys.exit(0)

if kind == "pods":
    if not state.get("pods_query_ok", True):
        sys.exit(1)
    pods = state.get("pods", [])
    if "-A" in args and "--no-headers" in args:
        emit("{} {} {} {} {} {}".format(p["ns"], p["name"], p.get("ready", "1/1"),
                                        p["status"], p.get("restarts", "0"), "5m")
             for p in pods)
    elif "-A" in args:
        emit(f"{p['ns']}/{p['name']}\t{p.get('node', '')}" for p in pods)
    elif "{.spec.nodeName}{\"\\n\"}" in jsonpath():
        # Job-pod lookup: node names only, for one namespace.
        ns = flag("-n")
        emit(p.get("node", "") for p in pods if p["ns"] == ns)
    else:
        ns = flag("-n")
        emit(f"{p['name']}\t{p.get('node', '')}" for p in pods if p["ns"] == ns)
    sys.exit(0)

if kind == "deployment":
    name, ns = args[2], flag("-n")
    avail, desired = state.get("deployments", {}).get(f"{ns}/{name}", [1, 1])
    print(f"{avail} {desired}")
    sys.exit(0)

if kind == "jobs":
    if not state.get("jobs_query_ok", True):
        sys.exit(1)
    emit("{}/{}\t{}\t{}".format(j["ns"], j["name"], j.get("owner", "<none>"),
                                j.get("conditions", ""))
         for j in state.get("jobs", []))
    sys.exit(0)

sys.exit(0)
'''

STUB_SLEEP = "#!/bin/sh\nexit 0\n"

HEALTHY = {
    "nodes": [{"name": "k3s-server-01", "status": "Ready"},
              {"name": "k3s-agent-01", "status": "Ready"}],
    "pods": [{"ns": "kube-system", "name": "coredns-abc", "status": "Running",
              "node": "k3s-agent-01"}],
    "jobs": [],
}


def _stub_bin(tmp_path: Path) -> Path:
    binned = tmp_path / "bin"
    binned.mkdir()
    for name, body in (("kubectl", STUB_KUBECTL), ("sleep", STUB_SLEEP)):
        path = binned / name
        path.write_text(body)
        path.chmod(0o755)
    return binned


def run_verify(tmp_path: Path, state: dict, script: Path = SCRIPT,
               **env_over: str) -> subprocess.CompletedProcess:
    fixture = tmp_path / "state.json"
    fixture.write_text(json.dumps(state))
    binned = _stub_bin(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{binned}:{os.environ['PATH']}",
        "STUB_STATE": str(fixture),
        **env_over,
    }
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=env, timeout=120)


@pytest.fixture(autouse=True)
def _needs_python3():
    if shutil.which("python3") is None:
        pytest.skip("the kubectl stub needs python3 on PATH")


def test_a_healthy_cluster_passes(tmp_path):
    res = run_verify(tmp_path, HEALTHY)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Post-Maintenance Verification Passed" in res.stdout


def test_a_not_ready_node_fails_the_run(tmp_path):
    state = {**HEALTHY, "nodes": [{"name": "k3s-agent-01", "status": "NotReady"}]}
    res = run_verify(tmp_path, state)
    assert res.returncode == 1
    assert "ERROR: node k3s-agent-01 not Ready" in res.stdout
    assert "VERIFICATION FAILED" in res.stdout


def test_a_node_kured_is_rebooting_only_warns(tmp_path):
    # Annotated AND cordoned is the kured reboot signature; it must not fail the
    # run, or every maintenance window reds its own verification.
    state = {**HEALTHY, "nodes": [{"name": "k3s-agent-01", "status": "NotReady",
                                   "annot": "2026-01-01T12:00:00Z", "cordoned": True}]}
    res = run_verify(tmp_path, state)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "WARNING: node k3s-agent-01 NotReady (kured rebooting" in res.stdout


def test_an_unhealthy_pod_on_a_healthy_node_fails(tmp_path):
    state = {**HEALTHY,
             "pods": [{"ns": "media", "name": "radarr-1", "status": "CrashLoopBackOff",
                       "node": "k3s-agent-01"}]}
    res = run_verify(tmp_path, state)
    assert res.returncode == 1
    assert "ERROR: pod media/radarr-1 unhealthy" in res.stdout


def test_an_under_replicated_deployment_fails(tmp_path):
    state = {**HEALTHY, "deployments": {"traefik/traefik": [0, 2]}}
    res = run_verify(tmp_path, state)
    assert res.returncode == 1
    assert "traefik" in res.stdout and "VERIFICATION FAILED" in res.stdout


def test_a_failed_job_fails(tmp_path):
    state = {**HEALTHY,
             "jobs": [{"ns": "flux-system", "name": "pg-dump-1", "owner": "<none>",
                       "conditions": "Failed=True,"}]}
    res = run_verify(tmp_path, state)
    assert res.returncode == 1
    assert "ERROR: failed Job flux-system/pg-dump-1" in res.stdout


def test_a_query_failure_is_an_error_not_a_clean_run(tmp_path):
    # An empty answer and a broken API must not read alike: the second is the
    # one that would otherwise pass a maintenance window that verified nothing.
    state = {**HEALTHY, "jobs_query_ok": False}
    res = run_verify(tmp_path, state)
    assert res.returncode == 1
    assert "ERROR: failed to query Jobs" in res.stdout


def test_a_dns_probe_verdict_of_fail_fails(tmp_path):
    state = {**HEALTHY, "dns": "FAIL"}
    res = run_verify(tmp_path, state)
    assert res.returncode == 1
    assert "ERROR: Cluster DNS cannot resolve" in res.stdout


def test_the_suite_notices_a_lost_kured_excuse(tmp_path):
    """Mutation proof: drop the kured node list from the classifier and the
    excused-node case above must start failing."""
    mutant = tmp_path / "mutant.sh"
    body = SCRIPT.read_text().replace('classify_not_ready_nodes "$KURED_NOW"',
                                      'classify_not_ready_nodes ""')
    assert body != SCRIPT.read_text(), "the mutation target moved; re-point it"
    mutant.write_text(body)
    # The mutant sources its library from its own directory, so copy it along:
    # a missing source would exit 1 and the assertion would prove nothing.
    (tmp_path / "maintenance-lib.sh").write_text((SCRIPTS / "maintenance-lib.sh").read_text())
    state = {**HEALTHY, "nodes": [{"name": "k3s-agent-01", "status": "NotReady",
                                   "annot": "2026-01-01T12:00:00Z", "cordoned": True}]}
    res = run_verify(tmp_path, state, script=mutant)
    assert res.returncode == 1, "mutated script still passed — the test proves nothing"
