"""scripts/README.md's two script tables agree with scripts/vendored-manifest.yml.

The manifest is the machine-checked inventory and the README is the one a human
reads. When they disagree, an agent edits a library copy believing it is local.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
README = REPO / "scripts" / "README.md"
MANIFEST = REPO / "scripts" / "vendored-manifest.yml"

VENDORED_SECTION = "Vendored from weisssrv-lib"
LOCAL_SECTION = "Local helpers"

# A row's first cell is one or more backticked names: `a.sh`, or `a.sh`, `a-lib.sh`.
ROW = re.compile(r"^\|\s*((?:`[^`]+`,\s*)*`[^`]+`)\s*\|")
NAME = re.compile(r"`([^`]+)`")


def _sections() -> dict[str, list[str]]:
    """Heading -> the script names its table rows list."""
    found: dict[str, list[str]] = {}
    current = None
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            found.setdefault(current, [])
            continue
        match = ROW.match(line)
        if match and current is not None:
            found[current] += [Path(n).name for n in NAME.findall(match.group(1))]
    return found


def _consumer_paths(entries) -> set[str]:
    """Consumer-side paths of one manifest block (bare string, or lib/consumer)."""
    paths = set()
    for entry in entries or []:
        if isinstance(entry, str):
            paths.add(entry)
        elif isinstance(entry, dict):
            paths.add(entry.get("consumer") or entry["lib"])
    return paths


def manifest_script_names() -> set[str]:
    """Basenames of every copy and declared fork that lands under scripts/.

    The lint profiles and the molecule scaffolding land elsewhere and are not
    rows in either table.
    """
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    paths = _consumer_paths(doc.get("vendored")) | _consumer_paths(doc.get("forked"))
    return {Path(p).name for p in paths if p.startswith("scripts/")}


def test_both_tables_are_populated():
    """A regex that stopped matching would make every assertion below vacuous."""
    sections = _sections()
    assert len(sections[VENDORED_SECTION]) > 20
    assert len(sections[LOCAL_SECTION]) > 10


def undocumented(documented: set[str], manifest: set[str]) -> set[str]:
    """Manifest entries no row of the vendored table names."""
    return manifest - documented


def misfiled(local: set[str], manifest: set[str]) -> set[str]:
    """Library copies the README presents as this repository's own."""
    return local & manifest


def test_every_manifest_entry_is_in_the_vendored_table():
    sections = _sections()
    missing = undocumented(set(sections[VENDORED_SECTION]), manifest_script_names())
    assert not missing, (
        "scripts/vendored-manifest.yml carries copies that scripts/README.md "
        f"does not list under '{VENDORED_SECTION}': {sorted(missing)}"
    )


def test_no_library_copy_is_listed_as_a_local_helper():
    sections = _sections()
    wrong = misfiled(set(sections[LOCAL_SECTION]), manifest_script_names())
    assert not wrong, (
        f"scripts/README.md lists these under '{LOCAL_SECTION}', but "
        f"scripts/vendored-manifest.yml says they are library copies: {sorted(wrong)}"
    )


def test_a_dropped_row_is_caught():
    """Mutation case: a manifest entry missing from the vendored table fails."""
    manifest = manifest_script_names()
    documented = set(_sections()[VENDORED_SECTION]) - {"check-lib-pins.py"}
    assert undocumented(documented, manifest) == {"check-lib-pins.py"}


def test_a_reclassified_row_is_caught():
    """Mutation case: a library copy moved into the local table fails."""
    manifest = manifest_script_names()
    local = set(_sections()[LOCAL_SECTION]) | {"check-lib-pins.py"}
    assert misfiled(local, manifest) == {"check-lib-pins.py"}
