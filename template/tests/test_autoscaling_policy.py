"""The autoscaling policy file is shaped the way the gates read it.

check-hpa-vpa-invariant.py and validate-helm-values.py turn each allowlist key
into a set member verbatim, so a mis-shaped entry exempts nothing silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import SCRIPTS, load_script

POLICY = SCRIPTS / "autoscaling-policy.yaml"

# allowlist key -> the shape its entries must spell.
ALLOWLIST_SHAPES = {
    "cpu_limit_allowlist": "namespace/Kind/name",
    "vpa_cap_allowlist": "namespace/VerticalPodAutoscaler/name",
    "memory_ratio_allowlist": "namespace/Kind/name",
}


def _policy() -> dict:
    return yaml.safe_load(POLICY.read_text()) or {}


def entry_problems(key: str, doc: dict) -> list[str]:
    """What is wrong with one allowlist, as the gates would read it."""
    if key not in doc:
        return [f"{key} is absent — the gate reading it exempts nothing"]
    entries = doc[key]
    if not isinstance(entries, dict):
        return [
            f"{key} must be a mapping of \"{ALLOWLIST_SHAPES[key]}\": reason, "
            f"not {type(entries).__name__}"
        ]
    problems = []
    for entry, reason in entries.items():
        if str(entry).count("/") != 2:
            problems.append(f"{entry!r} is not {ALLOWLIST_SHAPES[key]}")
        if not isinstance(reason, str) or len(reason.split()) < 3:
            problems.append(f"{entry!r} needs a reason naming why it is exempt")
    return problems


def test_the_policy_loads_through_the_gate() -> None:
    policy = load_script("check-hpa-vpa-invariant.py").load_policy(str(POLICY))
    assert policy.chart_native_hpa_targets, (
        "the chart-native HPA list is empty — --require-chart-native-vpas would "
        "then assert nothing"
    )


@pytest.mark.parametrize("key", sorted(ALLOWLIST_SHAPES))
def test_every_allowlist_is_a_mapping_of_workload_keys(key: str) -> None:
    assert entry_problems(key, _policy()) == []


@pytest.mark.parametrize(
    "value",
    [[], ["downloads/Deployment/sabnzbd"], "downloads/Deployment/sabnzbd"],
)
def test_a_list_shaped_allowlist_is_reported(value) -> None:
    """Mutation case: a list exempts nothing, since the gates read keys."""
    assert entry_problems("cpu_limit_allowlist", {"cpu_limit_allowlist": value})


def test_an_entry_without_a_reason_is_reported() -> None:
    doc = {"vpa_cap_allowlist": {"downloads/VerticalPodAutoscaler/sabnzbd": "tbd"}}
    assert entry_problems("vpa_cap_allowlist", doc)


def test_an_entry_that_is_not_namespace_kind_name_is_reported() -> None:
    doc = {"memory_ratio_allowlist": {"sabnzbd": "pinned by the chart for now"}}
    assert entry_problems("memory_ratio_allowlist", doc)


def test_the_policy_file_is_where_the_gates_look() -> None:
    assert POLICY.is_file(), f"{POLICY} is gone, so every gate reading it is vacuous"
    assert Path(POLICY).suffix == ".yaml"
