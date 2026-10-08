#!/usr/bin/env python3
"""Coverage for scripts/deploy-verify.sh itself.

Each helper is extracted by name and driven in a bash subprocess, since the script
needs a live cluster to run whole. test_deploy_verify_lib.py covers the classifiers.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "deploy-verify.sh"


def extract(name: str) -> str:
    """The text of one shell function, from `name() {` to its closing brace."""
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def block(first: str, last: str) -> str:
    """The script text from the line starting with `first` up to `last`."""
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(first))
    end = next(i for i in range(start, len(lines)) if lines[i].startswith(last))
    return "\n".join(lines[start:end])


def bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          cwd=SCRIPTS.parent, timeout=60)


class TestWaitFor:
    """The retry helper every readiness gate is built on."""

    PRELUDE = extract("wait_for")

    def test_a_command_that_succeeds_returns_immediately(self):
        res = bash(f"{self.PRELUDE}\nwait_for 'thing' 30 5 true; echo rc=$?")
        assert "rc=0" in res.stdout
        assert "timed out" not in res.stdout

    def test_a_command_that_never_succeeds_times_out_and_reports(self):
        # The last attempt's output has to survive: a bare non-zero return here
        # leaves the pipeline with no diagnosis of what was still not ready.
        res = bash(
            f"{self.PRELUDE}\n"
            "probe() { echo 'still pending'; return 1; }\n"
            "wait_for 'thing' 0 1 probe; echo rc=$?"
        )
        assert "rc=1" in res.stdout
        assert "wait_for: thing timed out" in res.stdout
        assert "still pending" in res.stdout


class TestCheckNodesReady:
    """The first gate: an empty node list must never read as "all Ready"."""

    # Sources the real classifier: stubbing it would assert a composition the
    # script never ships.
    PRELUDE = f". {SCRIPTS}/deploy-verify-lib.sh\n" + extract("check_nodes_ready")

    def _run(self, node_output: str, tmp_path: Path) -> str:
        stub = tmp_path / "kubectl"
        stub.write_text(f"#!/bin/sh\nprintf '%s' '{node_output}'\n")
        stub.chmod(0o755)
        res = bash(f'PATH="{tmp_path}:$PATH"\n{self.PRELUDE}\n'
                   "check_nodes_ready; echo rc=$?")
        return res.stdout

    def test_all_ready_passes(self, tmp_path):
        assert "rc=0" in self._run("a Ready x\nb Ready x\n", tmp_path)

    def test_one_not_ready_fails(self, tmp_path):
        assert "rc=1" in self._run("a Ready x\nb NotReady x\n", tmp_path)

    def test_an_empty_answer_fails(self, tmp_path):
        assert "rc=1" in self._run("", tmp_path)

    def test_a_cordoned_node_is_not_ready(self, tmp_path):
        assert "rc=1" in self._run("a Ready,SchedulingDisabled x\n", tmp_path)


class TestKsReady:
    """The single Kustomization read both Kustomization loops share."""

    PRELUDE = extract("ks_ready")

    def _run(self, rc: int, tmp_path: Path) -> str:
        stub = tmp_path / "kubectl"
        stub.write_text(f"#!/bin/sh\n[ {rc} -eq 0 ] && printf 'True'\nexit {rc}\n")
        stub.chmod(0o755)
        res = bash(f'PATH="{tmp_path}:$PATH"\nset -euo pipefail\n'
                   f'{self.PRELUDE}\necho "got=$(ks_ready apps)"; echo rc=$?')
        return res.stdout

    def test_a_readable_condition_is_echoed(self, tmp_path):
        assert "got=True" in self._run(0, tmp_path)

    def test_an_unreadable_object_reads_Unknown_without_aborting(self, tmp_path):
        """Mutation case: without the `|| echo Unknown` the failed read kills
        the script under set -e and no Kustomization is ever reported."""
        out = self._run(1, tmp_path)
        assert "got=Unknown" in out
        assert "rc=0" in out


class TestCheckTopKustomizationsReady:
    """Anything but Ready=True — including a missing object — holds the gate."""

    PRELUDE = extract("ks_ready") + "\n" + extract("check_top_kustomizations_ready")

    def _run(self, ready: str, tmp_path: Path) -> str:
        stub = tmp_path / "kubectl"
        stub.write_text(f"#!/bin/sh\nprintf '%s' '{ready}'\n")
        stub.chmod(0o755)
        res = bash(f'PATH="{tmp_path}:$PATH"\n'
                   'TOP_KUSTOMIZATIONS="apps infrastructure-configs"\n'
                   f"{self.PRELUDE}\ncheck_top_kustomizations_ready; echo rc=$?")
        return res.stdout

    def test_ready_true_passes(self, tmp_path):
        assert "rc=0" in self._run("True", tmp_path)

    def test_ready_false_fails(self, tmp_path):
        assert "rc=1" in self._run("False", tmp_path)

    def test_an_absent_status_fails(self, tmp_path):
        assert "rc=1" in self._run("", tmp_path)


class TestRequireEnvsubstVars:
    """An empty FLUX_ENVSUBST_VARS would skip every server-side dry-run, which
    must be reported rather than read as a clean verification."""

    PRELUDE = extract("require_envsubst_vars")

    def _run(self, value: str) -> subprocess.CompletedProcess:
        return bash(f"FLUX_ENVSUBST_VARS='{value}'\n{self.PRELUDE}\n"
                    "require_envsubst_vars; echo rc=$?")

    def test_an_empty_allowlist_fails(self):
        res = self._run("")
        assert "rc=1" in res.stdout
        assert "would validate nothing" in res.stdout

    def test_a_populated_allowlist_passes(self):
        res = self._run("${cluster_internal_domain}")
        assert "rc=0" in res.stdout
        assert "would validate nothing" not in res.stdout

    def test_the_caller_holds_the_dry_run_on_an_empty_allowlist(self):
        """The guard is only useful if the main flow reacts to it."""
        assert "if ! require_envsubst_vars; then\n  EXIT=1" in SCRIPT.read_text()


def test_the_gated_stage_list_is_derived_not_hand_written():
    """A hand-listed stage set is the drift flux-child-kustomizations.py exists
    to prevent."""
    assert "flux-child-kustomizations.py" in SCRIPT.read_text()


def test_the_version_pins_are_asserted_before_anything_runs():
    """Run outside its CI job the script must abort, not download an unpinned
    kustomize."""
    body = SCRIPT.read_text()
    for var in ("KUSTOMIZE_VERSION", "KUSTOMIZE_SHA256", "PYYAML_VERSION"):
        assert f'{var}="${{{var}:?' in body, f"{var} lost its fail-loud binding"
    assert "sha256sum -c -" in body


class TestPreReconcileSnapshot:
    """A classifier that could not run must take the STRICT arm, not report a
    bootstrap cluster and relax every later check."""

    SNAPSHOT = block("KS_ERR=$(mktemp)", "STEADY_STATE=")

    def _drive(self, tmp_path: Path, payload: str) -> subprocess.CompletedProcess:
        stub = tmp_path / "kubectl"
        stub.write_text("#!/bin/sh\ncat <<'JSON'\n" + payload + "\nJSON\n")
        stub.chmod(0o755)
        script = (
            f'PATH="{tmp_path}:$PATH"\n'
            f". {SCRIPTS / 'deploy-verify-lib.sh'}\n"
            "EXIT=0\n"
            f"{self.SNAPSHOT}\n"
            'echo "EXIT=$EXIT NOT_READY=$PRE_KS_NOT_READY"\n'
        )
        return bash(script)

    def test_a_well_formed_list_is_classified(self, tmp_path):
        result = self._drive(tmp_path, '{"items": []}')
        assert "EXIT=0 NOT_READY=0" in result.stdout

    def test_malformed_json_takes_the_strict_arm(self, tmp_path):
        """Mutation case: without the sentinel guard the 999 reads as numeric,
        steady_state returns false, and the verify still exits 0."""
        result = self._drive(tmp_path, "{not json")
        assert "EXIT=1 NOT_READY=0" in result.stdout
        assert "could not classify the pre-reconcile Kustomization list" in result.stdout


class TestExternalSecretReadiness:
    """A classifier that could not run must fail the verify, not print its
    sentinel as a count and pass as a bootstrap warning."""

    BLOCK = block('if ! wait_for "ExternalSecrets ready"', "# Fail if any Flux resource")

    def _drive(self, tmp_path: Path, payload: str, steady: str) -> subprocess.CompletedProcess:
        stub = tmp_path / "kubectl"
        stub.write_text("#!/bin/sh\ncat <<'JSON'\n" + payload + "\nJSON\n")
        stub.chmod(0o755)
        script = (
            f'PATH="{tmp_path}:$PATH"\n'
            "set -euo pipefail\n"
            f". {SCRIPTS / 'deploy-verify-lib.sh'}\n"
            "EXIT=0\n"
            f"STEADY_STATE={steady}\n"
            "wait_for() { return 1; }\n"
            f"{self.BLOCK}\n"
            'echo "EXIT=$EXIT"\n'
        )
        return bash(script)

    def test_a_real_not_ready_count_warns_during_bootstrap(self, tmp_path):
        payload = '{"items": [{"metadata": {"namespace": "n", "name": "s"}, "status": {}}]}'
        result = self._drive(tmp_path, payload, "false")
        assert "WARNING: 1 ExternalSecret(s) not Ready" in result.stdout
        assert "EXIT=0" in result.stdout

    def test_malformed_json_fails_even_during_bootstrap(self, tmp_path):
        """Mutation case: without the sentinel guard the 999 prints as a count
        and the bootstrap arm downgrades an unknown readiness to a warning."""
        result = self._drive(tmp_path, "{not json", "false")
        assert "could not classify the ExternalSecret list" in result.stdout
        assert "EXIT=1" in result.stdout


class TestIngressVipAssertion:
    """A LoadBalancer Service listing proves nothing: a VIP nobody announces is
    total ingress loss, so both VIPs are asserted assigned AND announceable."""

    BLOCK = block("if VIP_CONF=$(", 'echo "=== Verification Complete')

    @pytest.fixture(autouse=True)
    def _jq_present(self) -> None:
        # Fail rather than skip: without jq the extracted block reports
        # "announced by nobody" and the failure reads as a real outage.
        assert shutil.which("jq"), (
            "jq is not installed, so the ingress-VIP assertion is unverified. "
            "Install jq in the job that runs this suite."
        )

    KUBECTL_STUB = (
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"get nodes"*) printf "%s" "$STUB_INGRESS_NODES" ;;\n'
        '  *"traefik traefik -o"*) printf "%s" "$STUB_PUBLIC_IP" ;;\n'
        '  *"traefik traefik-internal -o"*) printf "%s" "$STUB_INTERNAL_IP" ;;\n'
        "  *endpointslices*) printf '%s' \"$STUB_SLICES\" ;;\n"
        "esac\n"
    )

    def _drive(self, tmp_path: Path, *, public_ip: str = "203.0.113.100",
               internal_ip: str = "203.0.113.101", endpoint_node: str = "node-a",
               ingress_nodes: str = "node-a node-b",
               conditions: str | None = '{"ready":true}',
               config_rc: int = 0,
               steady: str = "true") -> subprocess.CompletedProcess:
        config = tmp_path / "cluster-config-value.sh"
        if config_rc:
            config.write_text(
                f"#!/bin/sh\necho 'ERROR: key absent' >&2\nexit {config_rc}\n")
        else:
            config.write_text("#!/bin/sh\necho '203.0.113.100 203.0.113.101 cluster.test'\n")
        config.chmod(0o755)
        kubectl = tmp_path / "kubectl"
        kubectl.write_text(self.KUBECTL_STUB)
        kubectl.chmod(0o755)
        # An absent `conditions` key is a different jq path from an empty one:
        # EndpointSlice leaves it out when the endpoint is ready.
        endpoint = '{"nodeName":"%s"}' % endpoint_node if conditions is None else (
            '{"nodeName":"%s","conditions":%s}' % (endpoint_node, conditions))
        slices = '{"items":[{"endpoints":[%s]}]}' % endpoint
        script = "\n".join([
            "set -uo pipefail",
            f'PATH={shlex.quote(str(tmp_path))}:$PATH',
            f'_SCRIPT_DIR={shlex.quote(str(tmp_path))}',
            f"STEADY_STATE={steady}",
            f"STUB_PUBLIC_IP={shlex.quote(public_ip)}",
            f"STUB_INTERNAL_IP={shlex.quote(internal_ip)}",
            f"STUB_INGRESS_NODES={shlex.quote(ingress_nodes)}",
            f"STUB_SLICES={shlex.quote(slices)}",
            "export STUB_PUBLIC_IP STUB_INTERNAL_IP STUB_INGRESS_NODES STUB_SLICES",
            "EXIT=0",
            self.BLOCK,
            'echo "EXIT=$EXIT"',
        ])
        return bash(script)

    def test_both_vips_assigned_and_announceable_passes(self, tmp_path):
        result = self._drive(tmp_path)
        assert "EXIT=0" in result.stdout, result.stdout + result.stderr
        assert "203.0.113.100 announced from node-a" in result.stdout

    def test_a_pending_vip_fails(self, tmp_path):
        result = self._drive(tmp_path, public_ip="")
        assert "EXIT=1" in result.stdout, result.stdout + result.stderr
        assert "holds '<pending>'" in result.stdout

    def test_a_ready_endpoint_off_the_ingress_nodes_fails(self, tmp_path):
        """The mutation the gate exists for: both VIPs hold their address while
        every Traefik replica sits where no speaker runs."""
        result = self._drive(tmp_path, endpoint_node="node-z")
        assert "EXIT=1" in result.stdout, result.stdout + result.stderr
        assert "announced by nobody" in result.stdout

    def test_a_bootstrap_cluster_only_warns(self, tmp_path):
        result = self._drive(tmp_path, public_ip="", steady="false")
        assert "EXIT=0" in result.stdout, result.stdout + result.stderr
        assert "WARNING: Service traefik/traefik" in result.stdout

    def test_a_not_ready_endpoint_is_not_an_announcer(self, tmp_path):
        """Mutation case: dropping the ready filter accepts a terminating
        replica as the announcer and passes a verify during an ingress outage."""
        result = self._drive(tmp_path, conditions='{"ready":false}')
        assert "EXIT=1" in result.stdout, result.stdout + result.stderr
        assert "announced by nobody" in result.stdout

    def test_an_endpoint_with_no_ready_condition_counts_as_ready(self, tmp_path):
        """Mutation case: `ready == true` would call a healthy cluster down,
        since a ready endpoint may omit the optional field."""
        result = self._drive(tmp_path, conditions=None)
        assert "EXIT=0" in result.stdout, result.stdout + result.stderr
        assert "203.0.113.100 announced from node-a" in result.stdout

    def test_an_unreadable_cluster_config_fails_without_aborting(self, tmp_path):
        """Mutation case: unwrapped, the absent key kills the script under
        set -e and the Verification Complete summary never prints."""
        result = self._drive(tmp_path, config_rc=1)
        assert "EXIT=1" in result.stdout, result.stdout + result.stderr
        assert "the VIP-announce gate did not run" in result.stdout
class TestToolPreflight:
    """Every cluster read is `|| true`, so an absent kubectl must fail up front."""

    PREFLIGHT = "for _tool in kubectl jq flux; do"

    def _drive(self, tmp_path: Path, present, kubectl_rc: int = 0):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in present:
            stub = bin_dir / tool
            # An absolute interpreter: PATH below holds nothing but this directory.
            stub.write_text(f"#!/bin/sh\nexit {kubectl_rc if tool == 'kubectl' else 0}\n")
            stub.chmod(0o755)
        body = block(self.PREFLIGHT, 'echo "=== Post-Deployment')
        return subprocess.run(
            ["bash", "-c", f"set -euo pipefail\nPATH={shlex.quote(str(bin_dir))}\n{body}"],
            capture_output=True, text=True, timeout=60,
        )

    def test_every_tool_present_and_reachable_passes(self, tmp_path):
        result = self._drive(tmp_path, ["kubectl", "jq", "flux"])
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("missing", ["kubectl", "jq", "flux"])
    def test_a_missing_tool_exits_2(self, tmp_path, missing):
        present = [t for t in ("kubectl", "jq", "flux") if t != missing]
        result = self._drive(tmp_path, present)
        assert result.returncode == 2, result.stdout + result.stderr
        assert f"needs {missing} on PATH" in result.stderr

    def test_an_unreachable_apiserver_exits_2(self, tmp_path):
        """Mutation case: without this arm an unauthorized kubectl reads clean."""
        result = self._drive(tmp_path, ["kubectl", "jq", "flux"], kubectl_rc=1)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "cannot reach the cluster" in result.stderr


class TestCheckObsPodsHealthy:
    """A refused lookup must hold the gate, not pass as an empty namespace."""

    PRELUDE = f". {SCRIPTS}/deploy-verify-lib.sh\n" + extract("check_obs_pods_healthy")

    def _run(self, body: str, tmp_path: Path) -> str:
        stub = tmp_path / "kubectl"
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
        res = bash(f'PATH="{tmp_path}:$PATH"\n{self.PRELUDE}\n'
                   "check_obs_pods_healthy; echo rc=$?")
        return res.stdout

    def test_a_failed_lookup_fails(self, tmp_path):
        assert "rc=1" in self._run("echo forbidden >&2; exit 1", tmp_path)

    def test_an_empty_namespace_fails(self, tmp_path):
        assert "rc=1" in self._run("exit 0", tmp_path)

    def test_all_running_and_ready_passes(self, tmp_path):
        assert "rc=0" in self._run(
            "printf '%s\\n' 'loki-0 1/1 Running 0 1d'", tmp_path)

    def test_a_crashlooping_pod_fails(self, tmp_path):
        assert "rc=1" in self._run(
            "printf '%s\\n' 'loki-0 0/1 CrashLoopBackOff 7 1d'", tmp_path)


def test_a_failed_observability_lookup_is_not_an_empty_namespace():
    """`|| true` / `|| echo '{"items":[]}'` here would report "nothing here yet,
    bootstrap" for a namespace the verifier was simply refused."""
    body = SCRIPT.read_text()
    assert "|| OBS_RC=$?" in body
    assert "|| OBS_HR_RC=$?" in body
    assert "--no-headers 2>/dev/null || true)" not in body
    assert """|| echo '{"items":[]}')""" not in body


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
