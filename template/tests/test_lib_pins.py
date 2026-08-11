"""The library pin gate: every weisssrv-lib `include:` matches one source.

GitLab resolves `include:` before the `variables:` block exists, so the ref has
to be a literal on every entry. `scripts/check-lib-pins.py` (vendored from the
library) reads `variables.WEISSSRV_LIB_REF` and requires that every entry pins
exactly that value, and that the value is a release tag. This runs it against
this repository's real pipeline; the exhaustive unit suite for the checker
itself lives in weisssrv-lib.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-lib-pins.py"
CI_FILE = REPO / ".gitlab-ci.yml"

_spec = importlib.util.spec_from_file_location("check_lib_pins", SCRIPT)
assert _spec and _spec.loader
check_lib_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_lib_pins)


def _project() -> str:
    """The library project this cluster includes from, as the pipeline declares
    it. Read rather than hardcoded so a fork or mirror is still gated."""
    variables = check_lib_pins.load_ci(CI_FILE).get("variables") or {}
    return variables.get("LIB_PROJECT") or check_lib_pins.LIB_PROJECT


def test_every_library_include_pins_the_single_source() -> None:
    problems = check_lib_pins.check(CI_FILE, _project())
    assert problems == [], "\n".join(problems)


def _want_ref() -> str:
    variables = check_lib_pins.load_ci(CI_FILE).get("variables") or {}
    want = variables.get("WEISSSRV_LIB_REF")
    assert want, "variables.WEISSSRV_LIB_REF is the single source and must be set"
    return str(want)


_MODULE_REF = re.compile(r'source\s*=\s*"git::[^"]*//terraform/modules/[^"?]+\?ref=([^"]+)"')


def test_terraform_module_sources_pin_the_same_ref() -> None:
    """The checker only reads `include:` entries, but the same tag is written on
    every Terraform module source. A module left on the old ref plans against a
    resource shape the rest of the repo has already moved off."""
    want = _want_ref()
    stale = [
        f"{path.relative_to(REPO)}: ?ref={ref}"
        for path in sorted((REPO / "terraform").glob("*/*.tf"))
        for ref in _MODULE_REF.findall(path.read_text(encoding="utf-8"))
        if ref != want
    ]
    assert not stale, f"Terraform module sources not pinned to {want}:\n" + "\n".join(stale)


_TASKFILE_LIB_REF = re.compile(r"^\s{2}LIB_REF:\s*(\S+)\s*$", re.MULTILINE)


def test_collection_and_taskfile_pin_the_same_ref() -> None:
    """The two pins `check-lib-pins.py --fix` does NOT repair.

    `ansible/requirements.yml` is the dangerous one: leave it behind and the CI
    templates come from the new tag while every deploy job installs the OLD role
    code onto the hosts — and the pipeline goes green either way. The Taskfile's
    `LIB_REF` is the local twin (the vendored-script refresh checks out that ref).
    """
    want = _want_ref()
    project = _project()
    stale: list[str] = []

    requirements = yaml.safe_load(
        (REPO / "ansible" / "requirements.yml").read_text(encoding="utf-8")
    )
    entries = [
        c
        for c in (requirements.get("collections") or [])
        if isinstance(c, dict)
        and str(c.get("type", "")) == "git"
        and project in str(c.get("name", ""))
    ]
    assert entries, f"ansible/requirements.yml pins no git collection from {project}"
    stale += [
        f"ansible/requirements.yml: {c['name']} version={c.get('version')}"
        for c in entries
        if str(c.get("version")) != want
    ]

    refs = _TASKFILE_LIB_REF.findall((REPO / "Taskfile.yml").read_text(encoding="utf-8"))
    assert refs, "Taskfile.yml declares no LIB_REF variable"
    stale += [f"Taskfile.yml: LIB_REF={ref}" for ref in refs if ref.strip("\"'") != want]

    assert not stale, f"library pins not on {want}:\n" + "\n".join(stale)
