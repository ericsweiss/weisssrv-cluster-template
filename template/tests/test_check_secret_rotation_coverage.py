"""Coverage for check-secret-rotation-coverage.py.

The live tree is clean, so it proves nothing about failure: every arm runs
against a fixture repo, and each mutation case must FAIL.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conftest import REPO, load_script

gate = load_script("check-secret-rotation-coverage.py")


EXTERNAL_SECRET = textwrap.dedent(
    """\
    apiVersion: external-secrets.io/v1beta1
    kind: ExternalSecret
    metadata:
      name: demo-secrets
      namespace: demo
    spec:
      data:
        - secretKey: token
          remoteRef:
            key: Demo Item
            property: token
    """
)


CLUSTER_EXTERNAL_SECRET = textwrap.dedent(
    """\
    apiVersion: external-secrets.io/v1
    kind: ClusterExternalSecret
    metadata:
      name: dns-api-token
    spec:
      externalSecretName: dns-api-token
      externalSecretSpec:
        data:
          - secretKey: api-token
            remoteRef:
              key: DNS Provider Token
              property: credential
    """
)


def write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(gate, "DECLARED_MANUAL", {})
    write(tmp_path, "kubernetes/apps/demo/externalsecret.yaml", EXTERNAL_SECRET)
    write(tmp_path, gate.DOC, "Demo Item rotates by hand: demo/demo-secrets\n")
    return tmp_path


def test_a_documented_secret_passes(repo: Path) -> None:
    assert gate.check(repo) == []
    assert gate.main(["--repo-root", str(repo)]) == 0


def test_an_undocumented_vault_item_fails(repo: Path) -> None:
    write(repo, gate.DOC, "demo/demo-secrets is refreshed by hand\n")
    problems = gate.check(repo)
    assert any("Demo Item" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_secret_no_rotation_path_reaches_fails(repo: Path) -> None:
    write(repo, gate.DOC, "Demo Item lives in the vault\n")
    problems = gate.check(repo)
    assert any("reached by no rotation path" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_declared_manual_entry_is_enough(repo: Path, monkeypatch) -> None:
    write(repo, gate.DOC, "Demo Item lives in the vault\n")
    monkeypatch.setattr(gate, "DECLARED_MANUAL", {"demo/demo-secrets": "no procedure yet"})
    assert gate.check(repo) == []


def test_a_stale_declared_manual_entry_fails(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(gate, "DECLARED_MANUAL", {"gone/gone-secrets": "stale"})
    assert any("drop the stale entry" in p for p in gate.check(repo))


def test_no_external_secrets_is_vacuous(tmp_path: Path) -> None:
    (tmp_path / "kubernetes").mkdir()
    write(tmp_path, gate.DOC, "nothing\n")
    with pytest.raises(gate.Vacuous):
        gate.check(tmp_path)
    assert gate.main(["--repo-root", str(tmp_path)]) == 2


def test_the_live_tree_is_clean() -> None:
    assert gate.check(REPO) == []


def test_an_unparseable_manifest_is_reported_not_dropped(repo: Path) -> None:
    """A file that will not parse takes its ExternalSecrets out of coverage, so
    the gate has to name it rather than print a clean summary."""
    write(repo, "kubernetes/apps/demo/broken.yaml", "kind: [unclosed\n")
    problems = gate.check(repo)
    assert any("broken.yaml unparseable" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_cluster_external_secret_is_covered_too(repo: Path) -> None:
    """A ClusterExternalSecret fans one vault item out to many namespaces, so it
    needs the same documented rotation as a namespaced one."""
    write(repo, "kubernetes/infrastructure/configs/shared/dns-api-token.yaml",
          CLUSTER_EXTERNAL_SECRET)
    problems = gate.check(repo)
    assert any("DNS Provider Token" in p for p in problems)
    assert gate.main(["--repo-root", str(repo)]) == 1

    write(repo, gate.DOC,
          "Demo Item rotates by hand: demo/demo-secrets\n"
          "DNS Provider Token rotates into dns-api-token\n")
    assert gate.check(repo) == []


def test_a_namespace_less_external_secret_is_reported(repo: Path) -> None:
    """A component's copy has no namespace of its own, so one entry would stand
    for every namespace that includes it and cover the rest silently."""
    body = EXTERNAL_SECRET.replace("  namespace: demo\n", "")
    write(repo, "kubernetes/components/demo-shared/externalsecret.yaml", body)
    problems = gate.check(repo)
    assert any("declares no metadata.namespace" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_non_utf8_rotation_doc_exits_2(repo: Path, capsys) -> None:
    """A doc the gate cannot decode is an operator error, not zero coverage."""
    (repo / gate.DOC).write_bytes(b"Demo \xff\xfe Item\n")
    assert gate.main(["--repo-root", str(repo)]) == 2
    assert "could not read a file" in capsys.readouterr().err


def test_a_non_utf8_manifest_is_reported_not_dropped(repo: Path) -> None:
    write(repo, "kubernetes/apps/demo/other.yaml", "")
    (repo / "kubernetes/apps/demo/other.yaml").write_bytes(b"kind: \xff\xfe\n")
    problems = gate.check(repo)
    assert any("other.yaml unparseable" in p for p in problems)
