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


_MODULE_REF = re.compile(r'source\s*=\s*"git::[^"]*//terraform/modules/[^"?]+\?ref=([^"]+)"')


def test_terraform_module_sources_pin_the_same_ref() -> None:
    """The checker only reads `include:` entries, but the same tag is written on
    every Terraform module source. A module left on the old ref plans against a
    resource shape the rest of the repo has already moved off."""
    variables = check_lib_pins.load_ci(CI_FILE).get("variables") or {}
    want = variables.get("WEISSSRV_LIB_REF")
    assert want, "variables.WEISSSRV_LIB_REF is the single source and must be set"
    stale = [
        f"{path.relative_to(REPO)}: ?ref={ref}"
        for path in sorted((REPO / "terraform").glob("*/*.tf"))
        for ref in _MODULE_REF.findall(path.read_text(encoding="utf-8"))
        if ref != want
    ]
    assert not stale, f"Terraform module sources not pinned to {want}:\n" + "\n".join(stale)
