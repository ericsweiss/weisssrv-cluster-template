"""Prove the shipped LAN-fence gate can FAIL.

The rendered manifests pass it by construction, so only a mutated except-list
and a peer-less rule the config does not name show that it is armed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import load_script

import render_cluster

# The copy every generated cluster runs, so this is what a cluster's own egress
# edits are gated by. The library's byte-identity engine keeps it equal to
# weisssrv-lib.
GATE = render_cluster.REPO_ROOT / "template" / "scripts" / "check-netpol-except-parity.py"

# Both canonical lists come from the gate itself, so the fixtures cannot drift
# from the constants it compares against.
_parity = load_script(GATE)
CANONICAL_LISTS = [_parity.LAN_FENCE, _parity.RESERVED_FULL]
CANONICAL_IDS = ["lan-fence", "reserved-full"]


def _policy(egress: list[dict], name: str = "allow-egress-public") -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": "example-app"},
        "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": egress},
    }


def _fenced(excepts: list[str]) -> dict:
    return _policy([{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": excepts}}]}])


def _write(tmp_path: Path, doc: dict) -> Path:
    directory = tmp_path / "kubernetes"
    directory.mkdir(exist_ok=True)
    (directory / "networkpolicy.yaml").write_text(yaml.safe_dump(doc))
    return directory


def _run(directory: Path, config: Path | None = None) -> subprocess.CompletedProcess:
    argv = [sys.executable, str(GATE)]
    if config:
        argv += ["--config", str(config)]
    return subprocess.run([*argv, str(directory)], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("canonical", CANONICAL_LISTS, ids=CANONICAL_IDS)
def test_the_canonical_fence_passes(tmp_path, canonical):
    result = _run(_write(tmp_path, _fenced(canonical)))
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_emptied_except_list_fails(tmp_path):
    """The edit that most directly re-opens the LAN."""
    assert all(CANONICAL_LISTS), "the gate exposed an empty canonical list"
    result = _run(_write(tmp_path, _fenced([])))
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("canonical", CANONICAL_LISTS, ids=CANONICAL_IDS)
def test_a_shortened_except_list_fails(tmp_path, canonical):
    """Dropping the metadata address alone is enough to fail it."""
    assert "169.254.0.0/16" in canonical
    shortened = [c for c in canonical if c != "169.254.0.0/16"]
    result = _run(_write(tmp_path, _fenced(shortened)))
    assert result.returncode == 1, result.stdout + result.stderr


def test_a_directory_with_no_policies_is_an_operator_error(tmp_path):
    """A renamed manifest subtree must red the gate, not pass it silently."""
    directory = tmp_path / "kubernetes"
    directory.mkdir()
    (directory / "configmap.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: not-a-policy\n"
    )
    result = _run(directory)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "scanned 0 NetworkPolicy manifests" in result.stderr


def _config(tmp_path: Path, allow: dict[str, str]) -> Path:
    path = tmp_path / "netpol-except.yaml"
    path.write_text(yaml.safe_dump({"unrestricted_egress_ok": allow}))
    return path


def test_an_undeclared_peer_less_rule_fails(tmp_path):
    """A rule with no `to:` allows every destination. The template renders the
    allowlist from the answers, so the arm reading it has to be armed too."""
    directory = _write(tmp_path, _policy([{"ports": [{"port": 443}]}], name="wide-open"))
    result = _run(directory, _config(tmp_path, {"example-app/other": "unrelated entry"}))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "no `to:` peers" in result.stdout + result.stderr


def test_peers_covering_a_fence_network_between_them_fail(tmp_path):
    """The containment arm: a hand-written rule reaching a fenced range in full
    is a LAN escape even when no single peer is a /0 with a drifted except-list,
    which is the only arm an operator's own narrow CIDRs can trip."""
    halves = _policy(
        [{"to": [{"ipBlock": {"cidr": "10.0.0.0/9"}}, {"ipBlock": {"cidr": "10.128.0.0/9"}}]}],
        name="split-halves",
    )
    result = _run(_write(tmp_path, halves))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "reaches all of 10.0.0.0/8" in result.stdout + result.stderr


def test_a_declared_peer_less_rule_passes(tmp_path):
    directory = _write(tmp_path, _policy([{"ports": [{"port": 443}]}], name="wide-open"))
    config = _config(tmp_path, {"example-app/wide-open": "deliberate, reviewed here"})
    result = _run(directory, config)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_config_entry_without_a_reason_is_an_operator_error(tmp_path):
    """An empty reason makes the allowlist unreviewable, so it is exit 2 rather
    than an accepted exemption."""
    directory = _write(tmp_path, _policy([{"ports": [{"port": 443}]}], name="wide-open"))
    result = _run(directory, _config(tmp_path, {"example-app/wide-open": ""}))
    assert result.returncode == 2, result.stdout + result.stderr
