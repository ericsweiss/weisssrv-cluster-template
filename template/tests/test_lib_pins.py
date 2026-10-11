"""Runs the vendored `scripts/check-lib-pins.py` against this repository's real
pipeline: every weisssrv-lib pin derives from the one `lib_ref` input the
pipeline header declares. The checker's own unit suite lives in the lib."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import REPO, lib_file, load_script

SCRIPT = REPO / "scripts" / "check-lib-pins.py"
CI_FILE = REPO / ".gitlab-ci.yml"

check_lib_pins = load_script(SCRIPT)


def _project() -> str:
    """The library project this cluster includes from, as the pipeline declares
    it. Read rather than hardcoded so a fork or mirror is still gated."""
    variables = check_lib_pins.load_ci(CI_FILE).get("variables") or {}
    return variables.get("LIB_PROJECT") or check_lib_pins.LIB_PROJECT


def _want_ref() -> str:
    want = check_lib_pins.declared_ref(CI_FILE.read_text(encoding="utf-8"))
    assert want, "spec.inputs.lib_ref.default is the single source and must be set"
    return str(want)


def test_every_library_pin_derives_from_the_declared_ref() -> None:
    """No CI job runs the checker here, so this is the pipeline's own arm over
    all four pin families: the includes, the variable, the collection and every
    site `scripts/lib-ref-sites.yml` declares."""
    want = _want_ref()
    project = _project()
    problems = (
        check_lib_pins.check(CI_FILE, project)
        + check_lib_pins.check_requirements(CI_FILE, want, project)
        + check_lib_pins.check_sites(CI_FILE, want)
    )
    assert problems == [], "\n".join(problems)


_MODULE_REF = re.compile(r'source\s*=\s*"git::[^"]*//terraform/modules/[^"?]+\?ref=([^"]+)"')


needs_terraform = pytest.mark.skipif(
    not (REPO / "terraform").is_dir(), reason="this repository ships no terraform/"
)


def _module_refs(root: Path) -> list[tuple[Path, str]]:
    """(file, ref) for every weisssrv-lib module source under `root`."""
    return [
        (path, ref)
        for path in sorted(root.rglob("*.tf"))
        for ref in _MODULE_REF.findall(path.read_text(encoding="utf-8"))
    ]


def _terraform_roots(tree: Path) -> list[Path]:
    """Every Terraform root under `tree`: a directory holding a .tf file. The set
    is answer-driven, so only the roots this cluster enabled are listed."""
    if not tree.is_dir():
        return []
    return sorted(p for p in tree.iterdir() if p.is_dir() and any(p.glob("*.tf")))


TERRAFORM_ROOTS = _terraform_roots(REPO / "terraform")


@needs_terraform
def test_the_terraform_roots_are_discovered() -> None:
    """A root the walk misses takes its pin out of the check below with it."""
    assert TERRAFORM_ROOTS, (
        "terraform/ ships no directory with a .tf file — the per-root pin check "
        "is examining nothing"
    )
    stray = [
        str(path.relative_to(REPO))
        for path in sorted((REPO / "terraform").glob("*.tf"))
    ]
    assert not stray, (
        f"Terraform files outside a root directory: {stray} — move them into a "
        "root so their module pins are checked."
    )


@needs_terraform
@pytest.mark.parametrize("root", TERRAFORM_ROOTS, ids=lambda p: p.name)
def test_every_terraform_root_is_declared_as_a_site(root: Path) -> None:
    """The site list is what holds each `?ref=` to the declared ref, and it
    cannot see a root it does not name: a new root would ship ungated."""
    found = _module_refs(root)
    assert found, (
        f"terraform/{root.name} pins no weisssrv-lib module source — either it "
        "stopped calling the module, or the source spelling changed and this "
        "root is no longer gated."
    )
    main_tf = root / "main.tf"
    globs = [glob for glob, _pattern in check_lib_pins.load_sites(CI_FILE)]
    covered = [glob for glob in globs if main_tf in set(REPO.glob(glob))]
    assert covered, (
        f"{main_tf.relative_to(REPO)} is matched by none of {globs} in "
        "scripts/lib-ref-sites.yml, so its module pin is not checked"
    )


def test_a_root_pinning_no_library_module_is_reported(tmp_path) -> None:
    """Mutation case: the per-root walk must see a pin, and see its absence."""
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "main.tf").write_text(
        'module "x" {\n  source = "./vendored"\n}\n', encoding="utf-8"
    )
    (tmp_path / "pinned").mkdir()
    (tmp_path / "pinned" / "main.tf").write_text(
        'module "x" {\n  source = "git::https://host/g/lib.git'
        '//terraform/modules/demo?ref=v1.2.3"\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "README.md").write_text("", encoding="utf-8")
    assert [p.name for p in _terraform_roots(tmp_path)] == ["local", "pinned"]
    assert _module_refs(tmp_path / "local") == []
    assert [ref for _path, ref in _module_refs(tmp_path / "pinned")] == ["v1.2.3"]


def test_the_taskfile_derives_the_ref_from_the_declared_input() -> None:
    """`task lib:sync` and the molecule image must read the pipeline's declared
    ref, not a second literal only a local run sees."""
    text = (REPO / "Taskfile.yml").read_text(encoding="utf-8")
    assert "scripts/check-lib-pins.py --print" in text, (
        "Taskfile.yml no longer derives LIB_REF from "
        "`scripts/check-lib-pins.py --print` — a literal there is a second pin "
        "that only local runs read"
    )
    literal = re.search(r"^\s{2}LIB_REF:\s*(v\d+\.\d+\.\d+)\s*$", text, re.MULTILINE)
    assert not literal, f"Taskfile.yml pins LIB_REF as the literal {literal.group(1)!r}"


SCENARIOS = REPO / "ansible" / "integration-tests"

needs_scenarios = pytest.mark.skipif(
    not SCENARIOS.is_dir(), reason="this repository ships no integration scenarios"
)

_IMAGE_LINE = re.compile(r"^\s*image:\s*(.+?)\s*$", re.MULTILINE)
_BARE_IMAGE = '"${MOLECULE_TEST_IMAGE}"'


def _scenario_images(scenarios: Path) -> list[tuple[Path, str]]:
    return [
        (path, image)
        for path in sorted(scenarios.glob("*/molecule/*/molecule.yml"))
        for image in _IMAGE_LINE.findall(path.read_text(encoding="utf-8"))
    ]


@needs_scenarios
def test_every_scenario_takes_the_image_from_the_environment() -> None:
    """A `:vX.Y.Z` fallback would be a second pin only a local run reads: green
    pipeline, stale local image. The Taskfile wrapper exports the variable."""
    found = _scenario_images(SCENARIOS)
    assert found, (
        "no platform image under ansible/integration-tests/ — this gate is "
        "examining nothing"
    )
    stale = [
        f"{path.relative_to(REPO)}: {image}" for path, image in found if image != _BARE_IMAGE
    ]
    assert not stale, (
        f"platform images are not the bare {_BARE_IMAGE}:\n" + "\n".join(stale)
    )


def test_a_pinned_fallback_image_is_reported(tmp_path: Path) -> None:
    """Mutation case: the collector, not just the shipped tree."""
    scenario = tmp_path / "base" / "molecule" / "default"
    scenario.mkdir(parents=True)
    (scenario / "molecule.yml").write_text(
        '      image: "${MOLECULE_TEST_IMAGE:-registry.example.com/lib/molecule-test:v0.0.1}"\n'
    )
    assert [image for _path, image in _scenario_images(tmp_path)] != [_BARE_IMAGE]


# The lint job installs ansible-lint from the include's input; requirements.txt
# is the local copy of that one pin.
_ANSIBLE_LINT_INCLUDE = "ci/lint/ansible-lint.yml"
REQUIREMENTS = REPO / "requirements.txt"
# GitLab's custom tags are not YAML, so a plain safe_load cannot read a template.
_CILoader = check_lib_pins.ci_yaml.NullTagCILoader


def _lib_input_default(relpath: str, name: str) -> str:
    """An include template's `spec.inputs.<name>.default` at the pinned ref."""
    header = next(
        (
            doc
            for doc in yaml.load_all(lib_file(relpath, _want_ref()), Loader=_CILoader)
            if isinstance(doc, dict) and "spec" in doc
        ),
        {},
    )
    inputs = ((header.get("spec") or {}).get("inputs")) or {}
    default = (inputs.get(name) or {}).get("default")
    assert default, f"{relpath} declares no {name} default at the pinned ref"
    return str(default)


def _ansible_lint_job_version() -> str:
    """The ansible-lint the CI job installs: the include's own
    `ansible_lint_version` input, else the library template's default."""
    includes = [
        entry
        for entry in check_lib_pins.load_ci(CI_FILE).get("include") or []
        if isinstance(entry, dict)
        and str(entry.get("file", "")).lstrip("/") == _ANSIBLE_LINT_INCLUDE
    ]
    assert includes, f".gitlab-ci.yml no longer includes /{_ANSIBLE_LINT_INCLUDE}"
    passed = {
        str((entry.get("inputs") or {})["ansible_lint_version"])
        for entry in includes
        if (entry.get("inputs") or {}).get("ansible_lint_version") is not None
    }
    assert len(passed) <= 1, (
        f"the ansible-lint includes pass conflicting versions: {sorted(passed)}"
    )
    if passed:
        return passed.pop()
    return _lib_input_default(_ANSIBLE_LINT_INCLUDE, "ansible_lint_version")


def ansible_lint_drift(requirements_text: str, job_version: str) -> list[str]:
    """The local ansible-lint pin when it disagrees with the CI job's."""
    match = re.search(r"^ansible-lint==([^\s#]+)", requirements_text, re.MULTILINE)
    assert match, "requirements.txt no longer pins `ansible-lint==`"
    local = match.group(1)
    if local == job_version:
        return []
    return [
        f"requirements.txt pins ansible-lint=={local} but the CI job installs "
        f"{job_version}"
    ]


def test_the_ansible_lint_pin_matches_the_library_input_default() -> None:
    """`task ansible:lint` and the CI job run one ansible-lint version.

    The include passes no `ansible_lint_version`, so CI takes the library
    template's default and a drifted local pin applies a different rule set.
    """
    stale = ansible_lint_drift(
        REQUIREMENTS.read_text(encoding="utf-8"), _ansible_lint_job_version()
    )
    assert not stale, (
        "\n  ".join(stale)
        + "\n  They are one pin — bump requirements.txt, or pass "
        "ansible_lint_version on the include."
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ansible-lint==26.8.0\n", []),
        (
            "ansible-lint==26.7.0\n",
            [
                "requirements.txt pins ansible-lint==26.7.0 but the CI job "
                "installs 26.8.0"
            ],
        ),
    ],
    ids=["matching", "drifted"],
)
def test_a_drifted_ansible_lint_pin_is_reported(text, expected) -> None:
    """Mutation case: the comparison fires on a stale local pin, and only then."""
    assert ansible_lint_drift(text, "26.8.0") == expected
