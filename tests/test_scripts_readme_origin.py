"""The generated repo's scripts/README.md must agree with vendored-manifest.yml.

The manifest is the inventory of record, so a mismatch either invites an in-place
edit or hides a vendored copy. A companion check covers a gate's sibling modules.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import yaml

import md_tables

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "template" / "scripts" / "README.md.jinja"
MANIFEST = REPO_ROOT / "scripts" / "vendored-manifest.yml"
RENDERED_SCRIPTS = REPO_ROOT / "template" / "scripts"

VENDORED_HEADING = "## Vendored from weisssrv-lib"

# The two shapes a gate loads a companion by: a sibling file read by name, or a
# plain import of a module that travels next to it.
_SIBLING_LITERAL = re.compile(r'"([A-Za-z0-9_-]+\.py)"')
_STDLIB = getattr(sys, "stdlib_module_names", frozenset())


def documented_vendored() -> set[str]:
    """Script names in the README's vendored table. A row may name two."""
    return md_tables.table_names(README.read_text(encoding="utf-8"), VENDORED_HEADING)


def registered_vendored() -> set[str]:
    """Script basenames the manifest registers under template/scripts/."""
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    names: set[str] = set()
    for block in ("vendored", "forked"):
        for entry in doc.get(block) or []:
            path = entry if isinstance(entry, str) else (entry.get("consumer") or entry["lib"])
            if path.startswith("template/scripts/"):
                names.add(Path(path).name)
    return names


def mismatches(documented: set[str], registered: set[str]) -> dict[str, str]:
    """name -> what is wrong with it, for every script the two sets disagree on."""
    return {
        **{n: "listed as vendored, not registered" for n in sorted(documented - registered)},
        **{n: "registered, missing from the vendored table" for n in sorted(registered - documented)},
    }


def test_the_tables_are_not_empty():
    """A regex that stopped matching would make the assertions below vacuous."""
    assert len(documented_vendored()) > 15
    assert len(registered_vendored()) > 15


def test_every_documented_vendored_script_is_registered():
    wrong = mismatches(documented_vendored(), registered_vendored())
    assert not wrong, (
        "template/scripts/README.md and scripts/vendored-manifest.yml disagree:\n  "
        + "\n  ".join(f"{name}: {why}" for name, why in wrong.items())
    )


def test_an_unregistered_script_is_caught():
    """Mutation case: a script the README calls vendored with no manifest entry."""
    assert mismatches(documented_vendored() | {"check-invented.py"}, registered_vendored()) == {
        "check-invented.py": "listed as vendored, not registered"
    }


def _missing_consumers(doc: dict, root: Path) -> list[str]:
    """Manifest entries whose consumer path is not a file under `root`."""
    missing = []
    for block in ("vendored", "forked"):
        for entry in doc.get(block) or []:
            path = entry if isinstance(entry, str) else (entry.get("consumer") or entry["lib"])
            if not (root / path).is_file():
                missing.append(path)
    return missing


def test_every_vendored_consumer_path_exists():
    """A renamed or conditionally-named file leaves a dead manifest entry behind.

    Only the library's byte-identity engine would otherwise report it, and that
    runs on a --lib-path invocation.
    """
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    assert (doc.get("vendored") or []) and (doc.get("forked") or []), (
        "scripts/vendored-manifest.yml registers nothing — this gate examined nothing"
    )
    missing = _missing_consumers(doc, REPO_ROOT)
    assert not missing, (
        "scripts/vendored-manifest.yml registers paths that do not exist:\n  "
        + "\n  ".join(missing)
    )


def test_a_dead_consumer_path_is_caught():
    """Mutation case: what a renamed conditional directory leaves in the manifest."""
    doc = {"vendored": [{"lib": "scripts/check-doc-links.py", "consumer": "template/gone.py"}]}
    assert _missing_consumers(doc, REPO_ROOT) == ["template/gone.py"]


def _is_available(module: str) -> bool:
    """A module the test environment can import is a dependency, not a companion."""
    if module in _STDLIB:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def companions(name: str, text: str) -> set[str]:
    """The sibling modules `text` loads: files it reads by name, and imports that
    resolve nowhere but next to it."""
    found = {literal for literal in _SIBLING_LITERAL.findall(text) if literal != name}
    imported: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    return found | {f"{module}.py" for module in imported if not _is_available(module)}


def unregistered_companions() -> dict[str, list[str]]:
    """script -> the companions it needs that the manifest does not register."""
    registered = registered_vendored()
    gaps = {}
    for name in sorted(registered):
        path = RENDERED_SCRIPTS / name
        if path.suffix != ".py" or not path.is_file():
            continue
        missing = sorted(companions(name, path.read_text(encoding="utf-8")) - registered)
        if missing:
            gaps[name] = missing
    return gaps


def test_every_companion_module_is_registered():
    """A vendored gate that grows a companion must grow a manifest entry too, or
    the render ships the gate without the module it imports."""
    gaps = unregistered_companions()
    assert not gaps, (
        "template/scripts/ gates load modules scripts/vendored-manifest.yml does not "
        "register:\n  "
        + "\n  ".join(f"{name} needs {', '.join(missing)}" for name, missing in gaps.items())
    )


def test_an_unregistered_companion_import_is_caught():
    """Mutation case: the shape a library gate takes when it grows a companion.

    The module name is one nothing registers, so the gap is the companion
    itself and not a stale expectation about which modules are vendored.
    """
    source = "import gate_invented\nfrom gate_invented import load\n"
    found = companions("check-made-up.py", source)
    assert found == {"gate_invented.py"}
    assert found - registered_vendored() == {"gate_invented.py"}


def test_a_sibling_file_read_by_name_counts_as_a_companion():
    source = 'HPA = parent / "check-hpa-vpa-invariant.py"\nimport yaml\n'
    assert companions("validate-helm-values.py", source) == {"check-hpa-vpa-invariant.py"}


def test_a_row_naming_two_scripts_yields_both():
    """The row shape both gates rely on: one first cell, two backticked names."""
    table = "## Heading\n\n| Script | Used by |\n|---|---|\n| `a.py` and `b.sh` | a task |\n"
    assert md_tables.table_names(table, "## Heading") == {"a.py", "b.sh"}


SITE_DATA_HEADING = "## Site data"


def undeclared_baselines() -> set[str]:
    """`.txt` files under template/scripts/ the README's Site data table omits.

    validate_render's orphan scan admits site data either by suffix or by a row
    naming it, and `.txt` is outside its suffix allowlist.
    """
    declared = md_tables.table_names(
        README.read_text(encoding="utf-8"), SITE_DATA_HEADING, (".txt",)
    )
    shipped = {path.name for path in RENDERED_SCRIPTS.glob("*.txt") if path.is_file()}
    return shipped - declared


def test_every_shipped_baseline_file_is_declared_as_site_data():
    """An undeclared one reads as an orphan and reds validate-rendered-cluster."""
    assert undeclared_baselines() == set()


def test_an_undeclared_baseline_file_is_caught():
    """Mutation case: the gate must name a baseline no row declares."""
    declared = md_tables.table_names(
        README.read_text(encoding="utf-8"), SITE_DATA_HEADING, (".txt",)
    )
    assert {"invented-expected-skipped.txt"} - declared == {"invented-expected-skipped.txt"}
    assert declared


# --------------------------------------------------------------------------
# Every Python gate is either a registered library copy or template-owned
# --------------------------------------------------------------------------

# Gates, helper modules and shipped pytest suites this template owns outright.
# A library twin under one of these names is unregistered, so byte-identity
# holds it to nothing.
TEMPLATE_OWNED = {
    "template/scripts/check-ansible-service-names.py",
    "template/scripts/check-cluster-literals.py.jinja",
    "template/scripts/check-collection-pin-trigger.py",
    "template/scripts/check-deploy-host-coverage.py",
    "template/scripts/check-grafana-sidecar-init.py",
    "template/scripts/check-guest-endpoint-parity.py",
    "template/scripts/check-integration-matrix-coverage.py",
    "template/scripts/check-kustomization-coverage.py",
    "template/scripts/check-skill-refs.py",
    "template/scripts/check-tenant-wiring.py.jinja",
    "template/scripts/check-role-default-flips.py",
    "template/scripts/check-unmanaged-secrets.py.jinja",
    "template/scripts/check-tenant-traefik-isolation.py.jinja",
    "template/scripts/check-upstream-rule-mirror.py",
    "template/scripts/deploy-preflight.py",
    "template/scripts/flux-secret-consumers.py",
    "template/scripts/generate-host-log-staleness.py",
    "template/scripts/taskfile_tree.py",
    "template/scripts/version-registry.py.jinja",
    "template/scripts/{% if use_unifi %}check-unifi-doc-parity.py{% endif %}",
    "template/scripts/{% if vpn_tailscale %}check-tailnet-dns-parity.py{% endif %}",
    "template/scripts/{% if vpn_tailscale %}check-tailscale-policy.py{% endif %}",
    # The suites that ship into the generated repository and run there.
    "template/scripts/test_check_ansible_service_names.py",
    "template/scripts/test_check_ci_pin_parity.py",
    "template/scripts/test_check_collection_pin_trigger.py",
    "template/scripts/test_check_deploy_host_coverage.py",
    "template/scripts/test_check_flux_version_pin.py",
    "template/scripts/test_check_integration_matrix_coverage.py",
    "template/scripts/test_collect_state_lib.py",
    "template/scripts/test_deploy_verify_lib.py",
    "template/scripts/test_deploy_verify_script.py",
    "template/scripts/test_diagnose_network_issues.py",
    "template/scripts/test_flux_secret_consumers.py",
    "template/scripts/test_maintenance_lib.py.jinja",
    "template/scripts/test_maintenance_run_with_verify.py",
    "template/scripts/test_post_maintenance_verify.py",
    "template/scripts/test_taskfile_tree.py.jinja",
}

SCRIPT_DIRS = ("scripts", "template/scripts")


def _consumer_paths(doc: dict) -> set[str]:
    """Every consumer path the manifest names, in either entry form."""
    return {
        (entry if isinstance(entry, str) else (entry.get("consumer") or entry["lib"]))
        for block in ("vendored", "forked")
        for entry in doc.get(block) or []
    }


def script_paths(root: Path) -> set[str]:
    """Every Python file under the two scripts/ trees, repo-relative.

    Matched on `.py` anywhere in the name: a conditional copy carries
    `{% endif %}` after the suffix, and a Jinja source carries `.jinja`.
    """
    return {
        path.relative_to(root).as_posix()
        for directory in SCRIPT_DIRS
        for path in (root / directory).iterdir()
        if path.is_file() and ".py" in path.name
    }


def unclassified(doc: dict, found: set[str]) -> list[str]:
    return sorted(found - _consumer_paths(doc) - TEMPLATE_OWNED)


def test_every_script_is_classified():
    """A library copy dropped into either scripts/ tree without a manifest entry
    is byte-compared by nothing, and a local edit to it survives every gate."""
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    found = script_paths(REPO_ROOT)
    assert len(found) > 40, f"the walk found only {len(found)} scripts — it matched nothing"
    gaps = unclassified(doc, found)
    assert not gaps, (
        "these scripts are neither registered in scripts/vendored-manifest.yml nor "
        f"declared template-owned: {gaps}"
    )


def test_an_unclassified_script_is_caught():
    """Mutation case: a library copy arriving under a name in neither set."""
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    found = script_paths(REPO_ROOT) | {"scripts/check-vendored-copies.py"}
    assert unclassified(doc, found) == ["scripts/check-vendored-copies.py"]


def test_a_template_owned_entry_that_no_longer_exists_is_caught():
    """TEMPLATE_OWNED is an exemption list, so a renamed file leaves a stale
    entry that would exempt the name it is next given."""
    stale = sorted(TEMPLATE_OWNED - script_paths(REPO_ROOT))
    assert not stale, f"TEMPLATE_OWNED names scripts this repository no longer ships: {stale}"
