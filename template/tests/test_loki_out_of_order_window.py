"""Host journald replay stays inside Loki's out-of-order accept window.

Loki rejects a line older than max_chunk_age / 2 behind its stream as
`too_far_behind`, so alloy_host_journal_max_age must fit inside that window.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import REPO, role_defaults, to_seconds

LOKI_RELEASE = REPO / "kubernetes/infrastructure/observability/loki/release.yaml"
INVENTORY = REPO / "ansible/inventories/prod"
VARIABLE = "alloy_host_journal_max_age"
ROLE = "alloy_host"
ALL_SCOPE = {("group_vars", "all.yml"), ("group_vars", "all.yaml"), ("group_vars", "all")}


def accept_window_seconds() -> float:
    values = yaml.safe_load(LOKI_RELEASE.read_text(encoding="utf-8"))["spec"]["values"]
    return to_seconds(values["loki"]["ingester"]["max_chunk_age"]) / 2


def _values_of(node, key: str):
    """Every value `key` takes at any depth: hosts.yml nests host vars."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                yield value
            else:
                yield from _values_of(value, key)
    elif isinstance(node, list):
        for item in node:
            yield from _values_of(item, key)


def journal_replays(inventory: Path, role_default: str) -> list[str]:
    """Every replay horizon a host can run with. The role default counts unless
    group_vars/all sets the variable, since only then does every host override it.
    Ansible loads both YAML suffixes, so the walk reads both."""
    found, set_for_all = [], False
    for path in sorted(p for suffix in ("*.yml", "*.yaml") for p in inventory.rglob(suffix)):
        values = [str(v) for v in _values_of(yaml.safe_load(path.read_text()), VARIABLE)]
        found += values
        rel = path.relative_to(inventory).parts
        set_for_all |= bool(values) and rel[:2] in ALL_SCOPE
    return found if set_for_all else [*found, role_default]


def _role_default() -> str:
    defaults = dict(role_defaults().get(VARIABLE, []))
    assert ROLE in defaults, f"{ROLE} no longer defaults {VARIABLE}"
    return str(defaults[ROLE])


def test_every_journal_replay_age_fits_the_accept_window():
    window = accept_window_seconds()
    too_old = [age for age in journal_replays(INVENTORY, _role_default())
               if to_seconds(age) > window]
    assert not too_old, (
        f"{VARIABLE} exceeds Loki's out-of-order window of {window / 3600:g}h "
        f"(max_chunk_age / 2), so replayed lines are rejected as too_far_behind: "
        f"{too_old}"
    )


def _inventory(root: Path, files: dict[str, dict]) -> Path:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(yaml.safe_dump(data))
    return root


def test_every_inventory_file_and_the_role_default_feed_the_replay(tmp_path):
    """Mutation case: a value in a narrower group, in host_vars under either YAML
    suffix or inline in hosts.yml is seen, and the role default counts until
    group_vars/all sets one."""
    inventory = _inventory(tmp_path, {
        "hosts.yml": {"all": {"hosts": {"a": {VARIABLE: "5h"}}}},
        "group_vars/proxmox.yml": {VARIABLE: "2h"},
        "host_vars/b.yml": {VARIABLE: "4h"},
        "host_vars/c.yaml": {VARIABLE: "6h"},
    })
    assert sorted(journal_replays(inventory, "3h")) == ["2h", "3h", "4h", "5h", "6h"]


@pytest.mark.parametrize("all_file", ["group_vars/all.yml", "group_vars/all.yaml",
                                      "group_vars/all/alloy.yml"])
def test_group_vars_all_replaces_the_role_default(tmp_path, all_file):
    """Mutation case: either suffix, or a file in the group_vars/all directory,
    sets the variable for every host, so the role default no longer applies."""
    inventory = _inventory(tmp_path, {all_file: {VARIABLE: "1h"}})
    assert journal_replays(inventory, "3h") == ["1h"]


def test_the_window_is_read_from_the_release():
    """A release that lost the key would fail the parse, not pass vacuously."""
    assert accept_window_seconds() > 0
