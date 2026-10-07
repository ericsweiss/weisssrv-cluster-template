"""Every file vendored from weisssrv-lib must still be byte-identical to it.

Copies and declared forks are listed in scripts/vendored-manifest.yml and checked
by the library's check-vendored-copies.py engine; contract: docs/ci-pipeline.md.
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path

import pytest
import yaml
from conftest import GATE_RELPATH, REPO, SCRIPTS, _lib_root

MANIFEST = SCRIPTS / "vendored-manifest.yml"

# Config files carry site data, not library code, so a same-named one is not a
# vendored copy.
_SITE_DATA_SUFFIXES = {".yml", ".yaml", ".env", ".conf", ".toml", ".json"}


class _CILoader(yaml.SafeLoader):
    """SafeLoader tolerating GitLab's `!reference` tags, subclassed so the
    constructor is not registered on the global SafeLoader."""


_CILoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _pinned_ref() -> str:
    ci = yaml.load((REPO / ".gitlab-ci.yml").read_text(), Loader=_CILoader) or {}
    ref = (ci.get("variables") or {}).get("WEISSSRV_LIB_REF")
    assert ref, ".gitlab-ci.yml variables.WEISSSRV_LIB_REF is the single source of the pin"
    return str(ref)


def _ref_available(lib: Path, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(lib), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


def _lib_offer(lib: Path, ref: str) -> set[str]:
    """The library's offer list at `ref`, or its working tree when the checkout
    has no such ref. A path absent from the offer is not yet registerable."""
    relpath = "scripts/vendorable-paths.yml"
    if _ref_available(lib, ref):
        blob = subprocess.run(
            ["git", "-C", str(lib), "show", f"{ref}:{relpath}"],
            capture_output=True,
            text=True,
        )
        raw = blob.stdout if blob.returncode == 0 else ""
    else:
        raw = (lib / relpath).read_text() if (lib / relpath).is_file() else ""
    return set((yaml.safe_load(raw) or {}).get("vendorable") or [])


def _run_gate(*extra: str) -> subprocess.CompletedProcess:
    lib = _lib_root()
    argv = [
        sys.executable,
        str(lib / GATE_RELPATH),
        "--manifest",
        str(MANIFEST),
        "--repo-root",
        str(REPO),
        "--lib-path",
        str(lib),
        *extra,
    ]
    return subprocess.run(argv, capture_output=True, text=True)


def registered_consumer_paths() -> list[str]:
    """Repo-relative paths registered as byte-identical copies (kind vendored).

    Empty with no library checkout: the never-skip assertion belongs to the gate
    tests below, not to a caller's collection time.
    """
    try:
        _lib_root()
    except AssertionError:
        return []
    result = _run_gate("--list")
    if result.returncode != 0:
        return []
    return [
        parts[1]
        for parts in (line.split("\t") for line in result.stdout.splitlines())
        if len(parts) >= 3 and parts[0] == "vendored"
    ]


@pytest.fixture(scope="module")
def registered() -> list[tuple[str, str, str]]:
    """(kind, consumer_path, lib_path) for every entry the manifest lists."""
    result = _run_gate("--list")
    assert result.returncode == 0, f"the library gate could not read {MANIFEST}:\n{result.stderr}"
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append((parts[0], parts[1], parts[2]))
    assert rows, f"{MANIFEST} declares no copies"
    return rows


@pytest.fixture(scope="session", autouse=True)
def announce_the_comparison_ref() -> None:
    """Drift reported against a ref the copies never came from misleads whoever
    re-vendors, so the fallback is announced rather than assumed. A fixture, not
    a test: it asserts nothing and would otherwise count as a passing check."""
    lib = _lib_root()
    ref = _pinned_ref()
    if not _ref_available(lib, ref):
        warnings.warn(
            f"{lib} has no {ref} (fetch its tags, or `task lib:sync`); byte-identity "
            "is compared against the checkout's working tree",
            stacklevel=1,
        )


def test_registered_copies_are_reconciled(registered) -> None:
    """Every vendored copy is identical and every fork reconciled, at the pinned ref."""
    ref = _pinned_ref()
    at_pin = _ref_available(_lib_root(), ref)
    result = _run_gate(*(["--ref", ref] if at_pin else []))
    assert result.returncode != 2, f"the vendored-copy gate could not run:\n{result.stderr}"
    if result.returncode == 0:
        return

    # Distinguish the two failures that read identically but need opposite
    # fixes: copies that match no version of the library (re-vendor them) from
    # copies that match its working tree while the pin lags (bump the pin).
    hint = (
        "Re-vendor from the .weisssrv-lib checkout (`task lib:sync`) and review the diff; "
        "site data belongs in the script's config file, never in the copy. A fork must "
        "ABSORB the library's change, then have its reconciled_sha256 updated in "
        "scripts/vendored-manifest.yml."
    )
    if at_pin and _run_gate().returncode == 0:
        hint = (
            f"These copies ARE current with the library working tree — they match it "
            f"exactly — but WEISSSRV_LIB_REF still pins {ref}, and the pin is what the "
            f"pipeline installs. Resolve it by bumping the pin (WEISSSRV_LIB_REF, "
            f"ansible/requirements.yml, Taskfile.yml's LIB_REF and the terraform module "
            f"?ref= pins) once a tag containing the change exists — not by re-vendoring "
            f"backwards."
        )
    raise AssertionError(f"{result.stdout}{result.stderr}\n{hint}")


def test_every_registered_copy_exists_here(registered) -> None:
    """A manifest entry is a claim about this repository's layout, so a moved or
    deleted copy has to surface here rather than as a silent no-op."""
    missing = sorted(path for _kind, path, _lib in registered if not (REPO / path).is_file())
    assert not missing, (
        f"listed as vendored/forked from weisssrv-lib but absent here: {missing}. "
        "Either restore them, or drop the entry from scripts/vendored-manifest.yml in "
        "the same commit — the manifest is this repository's to edit."
    )


def test_every_library_twin_is_registered(registered) -> None:
    """A script sharing a name with an OFFERED library script is covered.

    The offer list is read at the pinned ref, so a path only the library's working
    tree has does not red this repository.
    """
    lib = _lib_root()
    lib_names = {
        Path(path).name for path in _lib_offer(lib, _pinned_ref()) if path.startswith("scripts/")
    }
    assert lib_names, "the library offers no scripts at the pinned ref"
    covered = {Path(path).name for _kind, path, _lib in registered}
    undeclared = sorted(
        p.name
        for p in SCRIPTS.iterdir()
        if p.is_file()
        and p.name in lib_names
        and p.name not in covered
        and p.suffix not in _SITE_DATA_SUFFIXES
    )
    assert not undeclared, (
        "scripts with a weisssrv-lib twin that scripts/vendored-manifest.yml does not "
        f"cover: {undeclared} — add them there, or rename them so they are not "
        "mistaken for copies."
    )


# The local integration matrix hand-copies the library's privileged DinD daemon
# rather than taking it from an include input, so nothing else holds the two
# equal across a library bump.
INTEGRATION_JOBS = REPO / ".gitlab" / "ci" / "integration-jobs.yml"
DOCKER_BUILD_RELPATH = "ci/build/docker-build.yml"


def _lib_file(relpath: str) -> str:
    """A library file's text at the pinned ref, or from the checkout's working
    tree when that ref is not available locally."""
    lib = _lib_root()
    ref = _pinned_ref()
    if _ref_available(lib, ref):
        blob = subprocess.run(
            ["git", "-C", str(lib), "show", f"{ref}:{relpath}"],
            capture_output=True,
            text=True,
        )
        assert blob.returncode == 0, f"{relpath} is absent at {ref}:\n{blob.stderr}"
        return blob.stdout
    return (lib / relpath).read_text()


@pytest.mark.skipif(
    not INTEGRATION_JOBS.is_file(),
    reason="this repository ships no .gitlab/ci/integration-jobs.yml",
)
def test_the_dind_service_matches_the_library_input_default() -> None:
    """The integration matrix runs the same digest-pinned daemon as the library.

    It is the one privileged component of the job, and a stale copy keeps running
    the old build after the library bumps it.
    """
    text = _lib_file(DOCKER_BUILD_RELPATH)
    # A CI template file is two documents: the `spec.inputs` header, then the jobs.
    header = next(
        (
            doc
            for doc in yaml.load_all(text, Loader=_CILoader)
            if isinstance(doc, dict) and "spec" in doc
        ),
        {},
    )
    inputs = ((header.get("spec") or {}).get("inputs")) or {}
    default = (inputs.get("dind_service") or {}).get("default")
    assert default, f"{DOCKER_BUILD_RELPATH} no longer declares a dind_service default"

    jobs = yaml.load(INTEGRATION_JOBS.read_text(), Loader=_CILoader) or {}
    names = {
        service["name"] if isinstance(service, dict) else service
        for job in jobs.values()
        if isinstance(job, dict)
        for service in (job.get("services") or [])
    }
    dind = {str(name) for name in names if "dind" in str(name)}
    assert dind, f"no dind service found in {INTEGRATION_JOBS.relative_to(REPO)}"
    assert dind == {default}, (
        f"{INTEGRATION_JOBS.relative_to(REPO)} runs {sorted(dind)} but the library's "
        f"dind_service default at {_pinned_ref()} is {default!r} — they are one pin."
    )


# The collection tree the shared molecule scaffolding is offered from. No
# directory walk reaches ansible/molecule/, so its copies need their own check.
MOLECULE_SHARED = "ansible_collections/weisssrv/infra/molecule-shared"


def test_every_shared_molecule_twin_is_registered(registered) -> None:
    """A copy of the collection's molecule-shared/ YAML scaffolding is in the manifest."""
    offered = _lib_offer(_lib_root(), _pinned_ref())
    local = REPO / "ansible" / "molecule"
    if not local.is_dir():
        return
    covered = {path for _kind, path, _lib in registered}
    undeclared = sorted(
        str(p.relative_to(REPO))
        for p in local.rglob("*.yml")
        if p.is_file()
        and f"{MOLECULE_SHARED}/{p.relative_to(local)}" in offered
        and str(p.relative_to(REPO)) not in covered
    )
    assert not undeclared, (
        "copies of the collection's molecule-shared/ files that "
        f"scripts/vendored-manifest.yml does not cover: {undeclared} — register "
        "each one as vendored or forked."
    )
