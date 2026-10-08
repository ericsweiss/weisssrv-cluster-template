"""`terraform/authentik/import.sh --check` must agree with sso.tf.

import.sh derives its address table from imports.tf, so this catches a key
renamed in sso.tf and a parser that stops recognising a block shape.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTHENTIK = REPO_ROOT / "terraform" / "authentik"
IMPORT_SH = "import.sh"

# Resource type -> the sso.tf local map whose keys its addresses index.
RESOURCE_MAPS = {
    "authentik_provider_oauth2": "oauth2_providers",
    "authentik_provider_proxy": "proxy_providers",
    "authentik_provider_saml": "saml_providers",
    "authentik_group": "groups",
    "authentik_application": "applications",
    "authentik_policy_binding": "policy_bindings",
}

ADDRESS_RE = re.compile(r'^module\.sso\.([a-z0-9_]+)\.this\["([^"]+)"\]$')

# One import block of each shape the extractor claims to handle.
FIXTURE_IMPORTS_TF = """\
locals {
  imported_application_slugs = toset([
    "grafana",
  ])
}

import {
  for_each = local.imported_application_slugs
  to       = module.sso.authentik_application.this[each.value]
  id       = each.value
}

import {
  to = module.sso.authentik_provider_oauth2.this["grafana"]
  id = "12"
}

import {
  for_each = {
    "grafana-users" = "018f0000-0000-0000-0000-000000000000"
  }
  to = module.sso.authentik_group.this[each.key]
  id = each.value
}
"""


def run_check(directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", IMPORT_SH, "--check"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )


def check_lines(directory: Path) -> list[str]:
    result = run_check(directory)
    assert result.returncode == 0, f"{IMPORT_SH} --check failed:\n{result.stderr}"
    if not result.stdout:
        return []
    lines = result.stdout.split("\n")
    assert lines[-1] == "", "--check output is not newline-terminated"
    body = lines[:-1]
    assert all(line.strip() for line in body), f"--check printed a blank line: {body}"
    return [line for line in body if not line.startswith("#")]


def work_copy(tmp_path: Path, imports_tf: str | None = None) -> Path:
    """A throwaway copy of terraform/authentik, optionally with its own imports.tf."""
    work = tmp_path / "authentik"
    shutil.copytree(AUTHENTIK, work)
    if imports_tf is not None:
        (work / "imports.tf").write_text(imports_tf)
    return work


def top_level_keys(body: str, local_name: str) -> list[str]:
    """Keys declared directly inside `<local_name> = { ... }`.

    A brace-depth walk: quoted keys and nested objects both defeat an
    indentation regex, and a miscount here would pass a dead address.
    """
    match = re.search(rf"^\s*{re.escape(local_name)}\s*=\s*\{{", body, re.M)
    assert match, f"local.{local_name} is missing from sso.tf"
    depth = 1
    keys: list[str] = []
    for line in body[match.end():].split("\n"):
        stripped = line.strip()
        if depth == 1 and stripped and not stripped.startswith("#"):
            key = re.match(r'^("?)([A-Za-z0-9_.-]+)\1\s*=', stripped)
            if key:
                keys.append(key.group(2))
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
    return keys


def dead_addresses(addresses: list[str], sso_body: str) -> list[str]:
    """Addresses whose map key is absent from the sso.tf map they name."""
    dead = []
    for address in addresses:
        parsed = ADDRESS_RE.match(address)
        assert parsed, f"unrecognised import address: {address}"
        resource, key = parsed.groups()
        local_name = RESOURCE_MAPS.get(resource)
        assert local_name, f"{resource} has no sso.tf map in RESOURCE_MAPS"
        if key not in top_level_keys(sso_body, local_name):
            dead.append(address)
    return dead


def test_import_sh_ships_executable():
    script = AUTHENTIK / IMPORT_SH
    assert script.is_file(), f"{IMPORT_SH} is missing"
    assert os.access(script, os.X_OK), f"{IMPORT_SH} is not executable"


def test_check_prints_well_formed_import_pairs():
    pairs = check_lines(AUTHENTIK)
    addresses = []
    for pair in pairs:
        address, sep, identifier = pair.partition("|")
        assert sep and identifier, f"not an address|id pair: {pair!r}"
        assert ADDRESS_RE.match(address), f"malformed import address: {address!r}"
        addresses.append(address)
    duplicates = sorted({a for a in addresses if addresses.count(a) > 1})
    assert not duplicates, f"imports.tf binds these addresses twice: {duplicates}"


def test_every_import_address_names_a_live_sso_key():
    addresses = [pair.split("|", 1)[0] for pair in check_lines(AUTHENTIK)]
    sso_body = (AUTHENTIK / "sso.tf").read_text()
    dead = dead_addresses(addresses, sso_body)
    assert not dead, (
        f"{IMPORT_SH} imports addresses with no sso.tf key: {dead} — a renamed "
        "map key must be renamed in imports.tf too"
    )


def test_the_extractor_handles_every_block_shape_it_claims(tmp_path):
    work = work_copy(tmp_path, FIXTURE_IMPORTS_TF)
    assert check_lines(work) == [
        'module.sso.authentik_application.this["grafana"]|grafana',
        'module.sso.authentik_group.this["grafana-users"]'
        "|018f0000-0000-0000-0000-000000000000",
        'module.sso.authentik_provider_oauth2.this["grafana"]|12',
    ]


def test_the_parity_gate_rejects_an_address_whose_key_is_gone(tmp_path):
    """Mutation proof: a renamed sso.tf key must fail, not import nothing."""
    renamed = 'module.sso.authentik_group.this["renamed-grafana-users"]'
    work = work_copy(
        tmp_path,
        FIXTURE_IMPORTS_TF.replace('"grafana-users" =', '"renamed-grafana-users" ='),
    )
    addresses = [pair.split("|", 1)[0] for pair in check_lines(work)]
    sso_body = (AUTHENTIK / "sso.tf").read_text()
    assert dead_addresses(addresses, sso_body) == [renamed]


def test_an_unparsed_block_shape_aborts_instead_of_under_reporting(tmp_path):
    """Mutation proof: a block the extractor cannot read fails the run."""
    work = work_copy(
        tmp_path,
        'import {\n  to = module.sso.authentik_group.this["x"]\n  unknown = 1\n}\n',
    )
    result = run_check(work)
    assert result.returncode == 2, result.stdout
    assert "unrecognised line in an import block" in result.stderr


def test_the_floor_rejects_a_parser_that_misses_a_block(tmp_path):
    """Mutation proof: fewer pairs than imports.tf blocks must fail the run."""
    work = work_copy(tmp_path, FIXTURE_IMPORTS_TF)
    script = work / IMPORT_SH
    broken = script.read_text().replace(
        "/^import[ \\t]*\\{/ { inblock = 1", "/^never_matches/ { inblock = 1"
    )
    script.write_text(broken)
    result = run_check(work)
    assert result.returncode == 2, result.stdout
    assert "its block shape changed" in result.stderr


def test_check_survives_an_imports_file_with_no_blocks(tmp_path):
    """A fresh cluster has adopted nothing: an empty table, not a set -e abort."""
    work = work_copy(tmp_path, "# nothing adopted yet\n")
    result = run_check(work)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
