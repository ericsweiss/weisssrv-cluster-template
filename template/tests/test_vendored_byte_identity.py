"""Every file vendored from weisssrv-lib must still be byte-identical to it.

The library's check-vendored-copies.py engine checks scripts/vendored-manifest.yml
at the declared ref, or $WEISSSRV_LIB_REF_OVERRIDE. Contract: docs/ci-pipeline.md.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import (
    GATE_RELPATH,
    REPO,
    SCRIPTS,
    CILoader,
    _lib_root,
    lib_file,
    pinned_lib_ref,
    require_ref,
)

MANIFEST = SCRIPTS / "vendored-manifest.yml"

# Config files carry site data, not library code, so a same-named one is not a
# vendored copy.
_SITE_DATA_SUFFIXES = {".yml", ".yaml", ".env", ".conf", ".toml", ".json"}


def _lib_offer(lib: Path, ref: str) -> set[str]:
    """The library's offer list at `ref`. A path absent from the offer is not
    yet registerable, and an unreadable list fails rather than offering nothing."""
    wanted = require_ref(lib, ref)
    relpath = "scripts/vendorable-paths.yml"
    blob = subprocess.run(
        ["git", "-C", str(lib), "show", f"{wanted}:{relpath}"],
        capture_output=True,
        text=True,
    )
    assert blob.returncode == 0, f"{wanted}:{relpath} is not in {lib}:\n{blob.stderr}"
    offered = set((yaml.safe_load(blob.stdout) or {}).get("vendorable") or [])
    assert offered, f"{wanted}:{relpath} offers nothing, so every twin check is vacuous"
    return offered


def _run_gate(
    *extra: str, manifest: Path | None = None, repo_root: Path | None = None
) -> subprocess.CompletedProcess:
    lib = _lib_root()
    argv = [
        sys.executable,
        str(lib / GATE_RELPATH),
        "--manifest",
        str(manifest or MANIFEST),
        "--repo-root",
        str(repo_root or REPO),
        "--lib-path",
        str(lib),
        *extra,
    ]
    return subprocess.run(argv, capture_output=True, text=True)


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


def test_lib_checkout_carries_the_pinned_ref() -> None:
    """Drift against a ref the copies never came from misleads whoever
    re-vendors, so the declared ref has to resolve in the checkout."""
    require_ref(_lib_root(), pinned_lib_ref())


def test_registered_copies_are_reconciled(registered) -> None:
    """Every vendored copy is identical and every fork reconciled, at the pinned ref."""
    ref = require_ref(_lib_root(), pinned_lib_ref())
    result = _run_gate("--ref", ref)
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
    if _run_gate().returncode == 0:
        hint = (
            f"These copies ARE current with the library working tree — they match it "
            f"exactly — but the declared lib_ref still pins {ref}, and the pin is what "
            f"the pipeline installs. Resolve it with `scripts/check-lib-pins.py --bump "
            f"vX.Y.Z` once a tag containing the change exists — not by re-vendoring "
            f"backwards."
        )
    raise AssertionError(f"{result.stdout}{result.stderr}\n{hint}")


def test_the_engine_reports_a_mutated_copy(registered, tmp_path) -> None:
    """Mutation case: the live tree only exercises the clean direction, so the
    reporting branch is proven against a scratch copy taken from the library."""
    lib_path, consumer = next(
        (lib, path) for kind, path, lib in registered if kind == "vendored" and path.endswith(".py")
    )
    content = lib_file(lib_path, pinned_lib_ref())
    manifest = tmp_path / "scripts" / "vendored-manifest.yml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"vendored:\n  - lib: {lib_path}\n    consumer: {consumer}\n")
    copy = tmp_path / consumer
    copy.parent.mkdir(parents=True, exist_ok=True)
    at_pin = ["--ref", require_ref(_lib_root(), pinned_lib_ref())]

    copy.write_text(content)
    clean = _run_gate(*at_pin, manifest=manifest, repo_root=tmp_path)
    assert clean.returncode == 0, (
        f"a scratch copy taken from the library is reported as drifted:\n{clean.stdout}{clean.stderr}"
    )

    copy.write_text(content.replace("\n", "\n# drift\n", 1))
    drifted = _run_gate(*at_pin, manifest=manifest, repo_root=tmp_path)
    assert drifted.returncode == 1, (
        f"one mutated byte in {consumer} was not reported (exit {drifted.returncode}):\n"
        f"{drifted.stdout}{drifted.stderr}"
    )
    assert consumer in drifted.stdout + drifted.stderr, (
        f"the drift report does not name {consumer}:\n{drifted.stdout}{drifted.stderr}"
    )


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
        Path(path).name for path in _lib_offer(lib, pinned_lib_ref()) if path.startswith("scripts/")
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
    """A library file's text at the pinned ref."""
    return lib_file(relpath, pinned_lib_ref())


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
            for doc in yaml.load_all(text, Loader=CILoader)
            if isinstance(doc, dict) and "spec" in doc
        ),
        {},
    )
    inputs = ((header.get("spec") or {}).get("inputs")) or {}
    default = (inputs.get("dind_service") or {}).get("default")
    assert default, f"{DOCKER_BUILD_RELPATH} no longer declares a dind_service default"

    jobs = yaml.load(INTEGRATION_JOBS.read_text(), Loader=CILoader) or {}
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
        f"dind_service default at {pinned_lib_ref()} is {default!r} — they are one pin."
    )

    # The daemon's bridge MTU, held to the same default: --mtu covers the default
    # bridge, the default-network-opt the user-defined networks molecule creates.
    # 1500 black-holes large TLS frames and reads as a flaky registry or fetch.
    mtu = (inputs.get("dind_mtu") or {}).get("default")
    assert mtu, f"{DOCKER_BUILD_RELPATH} no longer declares a dind_mtu default"
    commands = [
        str(argument)
        for job in jobs.values()
        if isinstance(job, dict)
        for service in (job.get("services") or [])
        if isinstance(service, dict) and "dind" in str(service.get("name"))
        for argument in (service.get("command") or [])
    ]
    for flag in (
        f"--mtu={mtu}",
        f"--default-network-opt=bridge=com.docker.network.driver.mtu={mtu}",
    ):
        assert flag in commands, (
            f"{INTEGRATION_JOBS.relative_to(REPO)} passes the dind daemon {commands}, "
            f"missing {flag} — the library's dind_mtu default at {pinned_lib_ref()}."
        )


# The collection tree the shared molecule scaffolding is offered from. No
# directory walk reaches ansible/molecule/, so its copies need their own check.
MOLECULE_SHARED = "ansible_collections/weisssrv/infra/molecule-shared"


def test_every_shared_molecule_twin_is_registered(registered) -> None:
    """A copy of the collection's molecule-shared/ YAML scaffolding is in the manifest."""
    offered = _lib_offer(_lib_root(), pinned_lib_ref())
    local = REPO / "ansible" / "molecule"
    covered = {path for _kind, path, _lib in registered}
    if not local.is_dir():
        stranded = sorted(path for path in covered if path.startswith("ansible/molecule/"))
        assert not stranded, f"registered but ansible/molecule/ is absent: {stranded}"
        return
    assert any(path.startswith(f"{MOLECULE_SHARED}/") for path in offered), (
        f"the library offers nothing under {MOLECULE_SHARED}/, so this walk compares nothing"
    )
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
