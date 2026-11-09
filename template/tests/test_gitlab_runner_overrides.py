"""The shared runner's per-job resource overrides can only shrink a container.

Each *_overwrite_max_allowed ceiling must equal an explicit default, so a job
variable cannot grow a pod past the sizes the namespace quota is derived from.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from conftest import REPO, release_values

SHARED_RELEASE = REPO / "kubernetes" / "apps" / "gitlab-runner" / "release.yaml"
SUFFIX = "_overwrite_max_allowed"


def _kubernetes_table(release: Path) -> dict:
    config = release_values(release)["runners"]["config"]
    runners = tomllib.loads(config)["runners"]
    assert len(runners) == 1, f"{release}: expected one [[runners]] table"
    return runners[0]["kubernetes"]


def ceiling_problems(table: dict) -> list[str]:
    """Each ceiling without an equal explicit default."""
    problems = []
    for key, ceiling in sorted(table.items()):
        if not key.endswith(SUFFIX):
            continue
        default_key = key[: -len(SUFFIX)]
        if default_key not in table:
            problems.append(f"{key} has no explicit {default_key}")
        elif table[default_key] != ceiling:
            problems.append(f"{key} {ceiling} differs from {default_key} {table[default_key]}")
    return problems


shipped = pytest.mark.skipif(
    not SHARED_RELEASE.is_file(),
    reason="this cluster ships no shared gitlab-runner module (git_backend)",
)


@shipped
def test_the_shared_runner_has_resource_ceilings():
    table = _kubernetes_table(SHARED_RELEASE)
    assert any(key.endswith(SUFFIX) for key in table), "no ceilings found"
    for key in ("service_memory_limit", "service_memory_request"):
        assert f"{key}{SUFFIX}" in table, f"{key} has no override ceiling"


@shipped
def test_every_shared_runner_ceiling_equals_its_default():
    assert ceiling_problems(_kubernetes_table(SHARED_RELEASE)) == []


@pytest.mark.parametrize(
    "table,expected",
    [
        (
            {"service_memory_limit_overwrite_max_allowed": "4Gi"},
            ["service_memory_limit_overwrite_max_allowed has no explicit service_memory_limit"],
        ),
        (
            {"memory_limit": "4Gi", "memory_limit_overwrite_max_allowed": "6Gi"},
            ["memory_limit_overwrite_max_allowed 6Gi differs from memory_limit 4Gi"],
        ),
        ({"memory_limit": "4Gi", "memory_limit_overwrite_max_allowed": "4Gi"}, []),
    ],
)
def test_a_ceiling_above_or_without_its_default_is_reported(table, expected):
    assert ceiling_problems(table) == expected
