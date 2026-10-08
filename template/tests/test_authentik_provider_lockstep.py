"""The authentik Terraform provider is pinned to one exact version whose minor
matches `authentik_version` in group_vars/all.yml, and .terraform.lock.hcl locks
that same version. A provider ahead of the server carries unserved schema.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
VERSIONS_TF = REPO / "terraform" / "authentik" / "versions.tf"
LOCK = REPO / "terraform" / "authentik" / ".terraform.lock.hcl"
ALL_YML = REPO / "ansible" / "inventories" / "prod" / "group_vars" / "all.yml"

_PIN = re.compile(r'version\s*=\s*"([0-9]{4}\.[0-9]+\.[0-9]+)"')
_LOCK_FIELD = re.compile(r'^\s*(version|constraints)\s*=\s*"([^"]+)"', re.MULTILINE)
_PROVIDER_BLOCK = 'provider "registry.terraform.io/goauthentik/authentik" {'

needs_authentik_terraform = pytest.mark.skipif(
    not VERSIONS_TF.is_file(), reason="this repository ships no terraform/authentik/"
)


def provider_pin(versions_tf: str) -> str:
    """The exact provider version out of the `required_providers` block."""
    found = _PIN.findall(versions_tf)
    assert len(found) == 1, f"expected exactly one provider version pin, found {found}"
    return found[0]


def minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def lock_fields(lock: str) -> dict:
    """The authentik provider block's own fields. Read by position, so a second
    provider block in the file would otherwise move the comparison."""
    lines = lock.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(_PROVIDER_BLOCK)]
    assert len(starts) == 1, (
        f"expected exactly one goauthentik/authentik provider block, found {len(starts)} — "
        "this gate reads fields by position and would compare the wrong block"
    )
    block = lines[starts[0] :]
    end = next(i for i, line in enumerate(block[1:], 1) if line.startswith("}"))
    return dict(_LOCK_FIELD.findall("\n".join(block[: end + 1])))


def problems(versions_tf: str, server_version: str, lock: str) -> list:
    """Every way the pin, the server version and the lockfile can disagree."""
    pin = provider_pin(versions_tf)
    found = []
    if minor(pin) != minor(server_version):
        found.append(
            f"provider pin {pin} is not minor-locked to authentik_version {server_version}"
        )
    fields = lock_fields(lock)
    for field in ("version", "constraints"):
        if fields.get(field) != pin:
            found.append(f".terraform.lock.hcl {field} is {fields.get(field)!r}, want {pin!r}")
    return found


def server_version() -> str:
    data = yaml.safe_load(ALL_YML.read_text(encoding="utf-8"))
    version = data.get("authentik_version")
    assert version, "group_vars/all.yml must pin authentik_version"
    return str(version)


@needs_authentik_terraform
def test_provider_pin_server_version_and_lockfile_agree() -> None:
    found = problems(
        VERSIONS_TF.read_text(encoding="utf-8"),
        server_version(),
        LOCK.read_text(encoding="utf-8"),
    )
    assert found == [], "\n".join(found)


@needs_authentik_terraform
def test_a_mismatched_minor_and_a_stale_lock_both_fail() -> None:
    pin = provider_pin(VERSIONS_TF.read_text(encoding="utf-8"))
    ahead = f"2099.1.{pin.split('.')[-1]}"
    mutated = VERSIONS_TF.read_text(encoding="utf-8").replace(pin, ahead)
    found = problems(mutated, server_version(), LOCK.read_text(encoding="utf-8"))
    assert any("minor-locked" in p for p in found), found
    assert any("lock.hcl version" in p for p in found), found


def test_the_lock_reader_is_scoped_to_the_authentik_block() -> None:
    """Mutation case: a second provider block must not move the comparison, and
    the authentik block going missing must fail rather than read as empty."""
    authentik = (
        f"{_PROVIDER_BLOCK}\n"
        '  version     = "2026.8.0"\n'
        '  constraints = "2026.8.0"\n'
        '  hashes = [\n    "h1:x=",\n  ]\n'
        "}\n"
    )
    random = (
        'provider "registry.terraform.io/hashicorp/random" {\n'
        '  version     = "3.6.0"\n'
        '  constraints = "3.6.0"\n'
        "}\n"
    )
    fields = lock_fields(authentik + "\n" + random)
    assert fields == {"version": "2026.8.0", "constraints": "2026.8.0"}
    with pytest.raises(AssertionError, match="exactly one"):
        lock_fields(random)
