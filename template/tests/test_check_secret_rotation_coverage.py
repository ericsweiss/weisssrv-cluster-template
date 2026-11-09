"""This cluster's rotation-coverage policy, run through the vendored gate.

The gate's arms are tested in weisssrv-lib; this suite holds the site half:
the document, the section, the manual-rotation map and the live tree.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conftest import REPO, load_script

gate = load_script("check-secret-rotation-coverage.py")

DOC = "docs/RUNBOOKS.md"
# The section that owns each claim: a name in an alert table or a storage
# procedure is not a documented rotation.
SECTION = "Rotating a secret"
# ExternalSecrets rotated outside the runbook, each with a reason. Ships empty:
# an exemption is an edit here, and a stale one fails the gate.
DECLARED_MANUAL: dict[str, str] = {}

EXTERNAL_SECRET = textwrap.dedent(
    """\
    apiVersion: external-secrets.io/v1
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


def run(root: Path, declared: dict[str, str] = DECLARED_MANUAL) -> int:
    """The gate as this cluster invokes it."""
    argv = ["--repo-root", str(root), "--doc", DOC, "--section", SECTION]
    for name, reason in declared.items():
        argv.append(f"--declared-manual={name}={reason}")
    return gate.main(argv)


def runbook(body: str, elsewhere: str = "") -> str:
    """A RUNBOOKS.md with `body` in the rotation section, `elsewhere` after it."""
    return (
        "# Runbooks\n\n## Alerts that link here\n\nnone\n\n"
        f"## {SECTION}\n\n```bash\n# a shell comment, not a heading\n```\n\n{body}\n"
        "### What each ExternalSecret holds\n\nsee above\n\n"
        f"## Storage\n\n{elsewhere}\n"
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    manifest = tmp_path / "kubernetes/apps/demo/externalsecret.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(EXTERNAL_SECRET)
    (tmp_path / "docs").mkdir()
    (tmp_path / DOC).write_text(runbook("Demo Item rotates by hand: demo/demo-secrets\n"))
    return tmp_path


def test_the_live_tree_is_clean() -> None:
    problems, _keys, names = gate.check(
        REPO, ["kubernetes"], [DOC], DECLARED_MANUAL, [SECTION]
    )
    assert names, "the rendered tree ships no ExternalSecret, so the gate checked nothing"
    assert problems == [], "\n".join(problems)


def test_a_documented_secret_passes(repo: Path) -> None:
    assert run(repo) == 0


def test_a_name_outside_the_rotation_section_does_not_count(repo: Path) -> None:
    """Mutation case: both names appear, but only under another section."""
    (repo / DOC).write_text(
        runbook("nothing here\n", "Demo Item rotates by hand: demo/demo-secrets\n")
    )
    assert run(repo) == 1


def test_a_subsection_of_the_rotation_section_counts(repo: Path) -> None:
    (repo / DOC).write_text(
        runbook("see below\n").replace("see above", "Demo Item rotates by hand: demo/demo-secrets")
    )
    assert run(repo) == 0


def test_a_runbook_without_the_rotation_section_exits_2(repo: Path) -> None:
    (repo / DOC).write_text("# Runbooks\n\nDemo Item rotates by hand: demo/demo-secrets\n")
    assert run(repo) == 2


def test_a_declared_manual_entry_covers_the_external_secret(repo: Path) -> None:
    (repo / DOC).write_text(runbook("Demo Item lives in the vault\n"))
    assert run(repo) == 1
    assert run(repo, {"demo/demo-secrets": "no procedure yet"}) == 0
