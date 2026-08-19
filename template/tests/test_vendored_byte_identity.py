"""Every file vendored from weisssrv-lib must still be byte-identical to it.

Nothing else notices when a vendored copy drifts: the fix the library shipped is
simply absent, and the next refresh silently reverts whatever was edited here.

**The copy relationship is recorded in this repository's own manifest** —
`scripts/vendored-manifest.yml`, read by the library's
`scripts/check-vendored-copies.py` engine (the library knows nothing about its
consumers; its `scripts/vendorable-paths.yml` offer list only bounds what a
manifest may name). This module drives that engine rather than keeping a second
list: a file the library stops shipping reaches this gate at the next pin bump,
and a file copied in without a manifest entry is caught by the twin smoke test
below. It covers more than `scripts/` — the lint profiles at the repository
root and the secret-detection ruleset under `.gitlab/` are copies too — and it
distinguishes vendored copies from declared forks, asserting a fork still
differs AND that the library side has not moved since it was last reconciled.

The library checkout comes from `$WEISSSRV_LIB_PATH` (what the CI job's
`setup_command` clones), else the `.weisssrv-lib/` checkout `task lib:sync`
creates, else a sibling `../weisssrv-lib`. There is no skip-when-missing path:
an unavailable checkout fails, because a gate that quietly disables itself is
not a gate. Blobs are read at the ref `.gitlab-ci.yml` pins, falling back to the
checkout's working tree when that ref is not in it yet — which is what a local
checkout tracking the library's `main` looks like before the next tag is cut.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
MANIFEST = SCRIPTS / "vendored-manifest.yml"
GATE_RELPATH = "scripts/check-vendored-copies.py"
# Where `task lib:sync` puts the checkout (Taskfile.yml's LIB_DIR).
LOCAL_CHECKOUT = ".weisssrv-lib"

# Config files carry site data, not library code, so a same-named one is not a
# vendored copy.
_SITE_DATA_SUFFIXES = {".yml", ".yaml", ".env", ".conf", ".toml", ".json"}


def _lib_root() -> Path:
    candidates = []
    explicit = os.environ.get("WEISSSRV_LIB_PATH")
    if explicit:
        candidates.append(Path(explicit))
    candidates += [REPO / LOCAL_CHECKOUT, REPO.parent / "weisssrv-lib"]
    for candidate in candidates:
        if (candidate / GATE_RELPATH).is_file():
            return candidate
    raise AssertionError(
        f"no weisssrv-lib checkout with {GATE_RELPATH} found — run `task lib:sync` "
        f"(it clones one into {LOCAL_CHECKOUT}/ at the pinned ref) or set "
        "$WEISSSRV_LIB_PATH. This gate never skips: an ungated vendored copy is "
        "exactly the drift it exists to catch."
    )


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
    """Drift reported against a ref the copies never came from misleads whoever
    re-vendors, so the fallback is announced rather than assumed."""
    lib = _lib_root()
    ref = _pinned_ref()
    if not _ref_available(lib, ref):
        pytest.skip(
            f"{lib} has no {ref} (fetch its tags, or `task lib:sync`); byte-identity "
            "was compared against the checkout's working tree"
        )


def test_registered_copies_are_reconciled(registered) -> None:
    """The gate itself: every vendored copy identical, every fork still a fork
    and still reconciled against the library blob it was forked from.

    Compared at the PINNED ref, never at whatever the checkout happens to have.
    Copies that match a newer working tree while the pin names an older tag are
    a real inconsistency — the pipeline installs the pin — so that state fails
    here and is resolved by bumping the pin, not by relaxing the comparison.
    """
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
    """Local smoke test: a script sharing a name with a library script must be
    covered by the manifest.

    The library cannot notice a file this repository added; without this,
    copying a library script in by hand and never registering it leaves it
    ungated — the exact failure the gate exists to prevent.
    """
    lib = _lib_root()
    lib_names = {p.name for p in (lib / "scripts").iterdir() if p.is_file()}
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
