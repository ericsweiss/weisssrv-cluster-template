"""Runs the vendored `scripts/check-lib-pins.py` against this repository's real
pipeline: every weisssrv-lib `include:` pins the literal release tag held in
`variables.WEISSSRV_LIB_REF`. The checker's own unit suite lives in the lib."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import REPO, load_script

SCRIPT = REPO / "scripts" / "check-lib-pins.py"
CI_FILE = REPO / ".gitlab-ci.yml"

check_lib_pins = load_script(SCRIPT)


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
def test_every_terraform_root_pins_the_same_ref(root: Path) -> None:
    """Each root is a thin caller of one library module, checked per root: a
    whole-tree scan stays green while one root stops pinning anything at all."""
    want = _want_ref()
    found = _module_refs(root)
    assert found, (
        f"terraform/{root.name} pins no weisssrv-lib module source — either it "
        "stopped calling the module, or the source spelling changed and this "
        "root is no longer gated."
    )
    stale = [f"{p.relative_to(REPO)}: ?ref={r}" for p, r in found if r != want]
    assert not stale, f"Terraform module sources not pinned to {want}:\n" + "\n".join(stale)


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


_TASKFILE_LIB_REF = re.compile(r"^\s{2}LIB_REF:\s*(\S+)\s*$", re.MULTILINE)


def _repo_name(value: str) -> str:
    """The repository name in a Galaxy source URL or an include project path."""
    return value.split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _names_project(entry: dict, project: str) -> bool:
    """Whether a collection entry installs `project`. The include path resolves
    instance-locally, so a mirror or fork on another host matches on repository
    name rather than as a substring, as the library gate does."""
    return any(
        project in value or _repo_name(value) == _repo_name(project)
        for value in (str(entry.get("name", "")), str(entry.get("source", "")))
        if value
    )


def _is_git_collection(entry: dict) -> bool:
    source = str(entry.get("source") or entry.get("name") or "")
    return str(entry.get("type", "")) == "git" or source.startswith("git+")


def test_collection_and_taskfile_pin_the_same_ref() -> None:
    """`ansible/requirements.yml` and the Taskfile `LIB_REF` pin the same ref;
    `check-lib-pins.py --fix` does not repair either."""
    want = _want_ref()
    project = _project()
    stale: list[str] = []

    requirements = yaml.safe_load(
        (REPO / "ansible" / "requirements.yml").read_text(encoding="utf-8")
    )
    entries = [
        c
        for c in (requirements.get("collections") or [])
        if isinstance(c, dict) and _is_git_collection(c) and _names_project(c, project)
    ]
    assert entries, f"ansible/requirements.yml pins no git collection from {project}"
    stale += [
        f"ansible/requirements.yml: {c.get('name') or c.get('source')} "
        f"version={c.get('version')}"
        for c in entries
        if str(c.get("version")) != want
    ]

    refs = _TASKFILE_LIB_REF.findall((REPO / "Taskfile.yml").read_text(encoding="utf-8"))
    assert refs, "Taskfile.yml declares no LIB_REF variable"
    stale += [f"Taskfile.yml: LIB_REF={ref}" for ref in refs if ref.strip("\"'") != want]

    assert not stale, f"library pins not on {want}:\n" + "\n".join(stale)


def test_the_collection_selector_follows_a_mirror_not_a_stranger() -> None:
    """Mutation case: a mirror in another namespace, and a `source:` entry, are
    still selected; an unrelated collection never is."""
    project = "infra/weisssrv-lib"
    mirror = {
        "type": "git",
        "name": "git+https://git.nonesuch.invalid/platform/weisssrv-lib.git"
        "#/ansible_collections/weisssrv/infra",
    }
    assert _is_git_collection(mirror)
    assert _names_project(mirror, project)

    sourced = {
        "name": "weisssrv.infra",
        "source": "git+https://git.nonesuch.invalid/x/weisssrv-lib",
    }
    assert _is_git_collection(sourced)
    assert _names_project(sourced, project)

    stranger = {"name": "community.general", "version": "1.0.0"}
    assert not _is_git_collection(stranger)
    assert not _names_project(stranger, project)


_MOLECULE_IMAGE = re.compile(r"molecule-test:([A-Za-z0-9._-]+)")
SCENARIOS = REPO / "ansible" / "integration-tests"
TESTING_DOC = REPO / "ansible" / "TESTING.md"

needs_scenarios = pytest.mark.skipif(
    not SCENARIOS.is_dir(), reason="this repository ships no integration scenarios"
)


def _molecule_image_tags(scenarios: Path, doc: Path) -> list[tuple[Path, str]]:
    paths = sorted(scenarios.glob("*/molecule/*/molecule.yml"))
    if doc.is_file():
        paths.append(doc)
    return [
        (path, tag)
        for path in paths
        for tag in _MOLECULE_IMAGE.findall(path.read_text(encoding="utf-8"))
    ]


@needs_scenarios
def test_molecule_test_image_fallbacks_pin_the_same_ref() -> None:
    """The local fallback tag each scenario falls back to, and the command in
    ansible/TESTING.md. CI overrides the image, so only local runs read these —
    a stale tag tests roles this cluster does not install."""
    want = _want_ref()
    found = _molecule_image_tags(SCENARIOS, TESTING_DOC)
    assert found, (
        "no molecule-test image tag under ansible/integration-tests/ or in "
        "ansible/TESTING.md — this gate is examining nothing"
    )
    stale = [
        f"{path.relative_to(REPO)}: molecule-test:{tag}" for path, tag in found if tag != want
    ]
    assert not stale, f"molecule test-image tags not on {want}:\n" + "\n".join(stale)


def test_a_stale_molecule_image_tag_is_reported(tmp_path: Path) -> None:
    """Mutation case: the collector, not just the shipped tree."""
    scenario = tmp_path / "base" / "molecule" / "default"
    scenario.mkdir(parents=True)
    (scenario / "molecule.yml").write_text(
        '      image: "${MOLECULE_TEST_IMAGE:-registry.example.com/lib/molecule-test:v0.0.1}"\n'
    )
    found = _molecule_image_tags(tmp_path, tmp_path / "absent.md")
    assert [tag for _path, tag in found] == ["v0.0.1"]
