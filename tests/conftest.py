"""Shared helpers for the suites under tests/: copier.yml as every suite reads
it, the Jinja environment copier renders with, the hyphenated-gate loader, and
the weisssrv-lib checkout the library arms read."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tarfile
from pathlib import Path

import jinja2
import jinja2_ansible_filters
import pytest
import yaml

import render_cluster

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# copier.yml, its questions, and the `when: false` entries among them: shared
# machinery in scope for every question, never prompted and never recorded.
CONFIG = yaml.safe_load((REPO / "copier.yml").read_text(encoding="utf-8"))
QUESTIONS = {k: v for k, v in CONFIG.items() if not k.startswith("_")}
COMPUTED = {
    name
    for name, question in QUESTIONS.items()
    if isinstance(question, dict) and question.get("when") is False
}


def copier_env(**options) -> jinja2.Environment:
    """Jinja carrying the ansible filter set copier renders this template with.

    `options` pass through, for a loader or stricter undefined.
    """
    return jinja2.Environment(  # noqa: S701 - rendering this repo's own templates
        extensions=[jinja2_ansible_filters.AnsibleCoreFiltersExtension], **options
    )


def load_script(name: str | Path):
    """Import a hyphenated script under a module name Python accepts.

    A bare basename resolves under scripts/; a path is taken as given, for the
    gates that ship inside the template tree rather than in scripts/.
    """
    path = Path(name)
    path = path if path.is_absolute() else SCRIPTS / path
    spec = importlib.util.spec_from_file_location(
        path.name.replace("-", "_").removesuffix(".py"), path
    )
    assert spec and spec.loader, f"{path} is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lib_checkout(sentinel: str) -> Path | None:
    """A weisssrv-lib checkout holding scripts/<sentinel>, or None. A set
    `$WEISSSRV_LIB_PATH` is the only candidate and must hold the sentinel; the
    sibling ../weisssrv-lib is tried only when the variable is unset."""
    explicit = os.environ.get("WEISSSRV_LIB_PATH")
    if explicit:
        if not (Path(explicit) / "scripts" / sentinel).is_file():
            raise RuntimeError(f"WEISSSRV_LIB_PATH={explicit} holds no scripts/{sentinel}")
        return Path(explicit)
    sibling = REPO.parent / "weisssrv-lib"
    return sibling if (sibling / "scripts" / sentinel).is_file() else None


def skip_or_fail(reason: str) -> None:
    """Skip a library-arm test locally, fail it under $CI. A permanent skip in CI
    is a gate reporting success forever, and the CI job clones the library."""
    if os.environ.get("CI"):
        pytest.fail(f"{reason} — the gate would silently skip in CI")
    pytest.skip(reason)


def lib_tree_at(lib: Path, ref: str, dest: Path) -> Path | None:
    """The library's tree at `ref`, exported into `dest`, or None when the
    checkout lacks the ref. Any other git failure raises with git's stderr."""
    commit = f"{ref}^{{commit}}"
    probe = subprocess.run(
        ["git", "-C", str(lib), "rev-parse", "--verify", "--quiet", commit],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 1:
        return None
    if probe.returncode != 0:
        raise RuntimeError(f"git rev-parse {ref} in {lib} failed: {probe.stderr.decode().strip()}")
    archive = subprocess.run(
        ["git", "-C", str(lib), "archive", "--format=tar", commit],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise RuntimeError(f"git archive {ref} in {lib} failed: {archive.stderr.decode().strip()}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def pinned_lib(tmp_path: Path, gate: str, sentinel: str) -> Path:
    """The library tree at this repository's declared lib_ref, so a checkout on
    another branch never decides a verdict. Skips locally, fails in CI."""
    lib = lib_checkout(sentinel)
    if lib is None:
        skip_or_fail(f"no weisssrv-lib checkout for the {gate}; set WEISSSRV_LIB_PATH")
    ref = render_cluster.declared_lib_ref(REPO / ".gitlab-ci.yml")
    assert ref, ".gitlab-ci.yml declares no lib_ref input"
    pinned = lib_tree_at(lib, ref, tmp_path / "lib")
    if pinned is None:
        skip_or_fail(f"{lib} has no {ref} for the {gate}; fetch its tags")
    return pinned
