"""Prove the shipped rotation-coverage gate matches whole names only.

A substring test lets a longer vault item or ExternalSecret name document a
shorter one. The rendered tree passes by construction, so a near-miss arms it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import load_script

import render_cluster

GATE = render_cluster.REPO_ROOT / "template" / "scripts" / "check-secret-rotation-coverage.py"
_gate = load_script(GATE)


def _tree(tmp_path: Path, runbook: str) -> Path:
    (tmp_path / "kubernetes" / "apps").mkdir(parents=True)
    (tmp_path / "kubernetes" / "apps" / "externalsecret.yaml").write_text(
        "apiVersion: external-secrets.io/v1\n"
        "kind: ExternalSecret\n"
        "metadata:\n"
        "  name: app-secrets\n"
        "  namespace: example-app\n"
        "spec:\n"
        "  data:\n"
        "    - secretKey: token\n"
        "      remoteRef:\n"
        "        key: example-app-oidc\n"
        "        property: client-secret\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "RUNBOOKS.md").write_text(runbook)
    return tmp_path


COVERED = """# Runbooks

## Rotating a secret

| Item | Workload |
|---|---|
| `example-app-oidc` | `example-app/app-secrets` |
"""

# Every name is a strict prefix of the one the runbook actually carries.
LONGER_NAMES = """# Runbooks

## Rotating a secret

| Item | Workload |
|---|---|
| `example-app-oidc-legacy` | `example-app/app-secrets-legacy` |
"""


def test_whole_name_coverage_passes(tmp_path):
    assert _gate.check(_tree(tmp_path, COVERED)) == []


@pytest.mark.parametrize("needle", ["example-app-oidc", "example-app/app-secrets"])
def test_longer_name_does_not_cover_a_shorter_one(tmp_path, needle):
    problems = _gate.check(_tree(tmp_path, LONGER_NAMES))
    assert any(needle in p for p in problems), problems


def test_vacuous_tree_is_not_a_pass(tmp_path):
    (tmp_path / "kubernetes").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "RUNBOOKS.md").write_text(COVERED)
    with pytest.raises(_gate.Vacuous):
        _gate.check(tmp_path)
