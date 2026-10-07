"""The swap-clean interlock covers every unit the archive chain starts."""

from __future__ import annotations

import yaml
from conftest import REPO

NAS_VARS = REPO / "ansible" / "inventories" / "prod" / "group_vars" / "nas.yml"

ON_SUCCESS = "nas_storage_archive_backup_on_success_units"
CONFLICTS = "nas_storage_swap_clean_conflicting_units"


def uninterlocked(group_vars: dict) -> list[str]:
    """Units the archive chain starts that swap-clean would not stand down for.

    swap-clean shrinks the ARC and stops guests, so a chained unit missing from
    the conflict list can have both happen under an in-flight run.
    """
    chained = group_vars.get(ON_SUCCESS) or []
    conflicting = set(group_vars.get(CONFLICTS) or [])
    return sorted(unit for unit in chained if unit not in conflicting)


def test_swap_clean_conflicts_with_every_chained_unit():
    assert NAS_VARS.is_file(), f"{NAS_VARS} is missing — the NAS group vars are gone"
    group_vars = yaml.safe_load(NAS_VARS.read_text(encoding="utf-8")) or {}
    missing = uninterlocked(group_vars)
    assert not missing, (
        f"units in {ON_SUCCESS} but not {CONFLICTS}, so swap-clean can start "
        f"under them:\n  " + "\n  ".join(missing)
    )


def test_a_chained_unit_outside_the_conflict_list_is_reported():
    assert uninterlocked(
        {
            ON_SUCCESS: ["restic-offsite.service"],
            CONFLICTS: ["archive-backup.service", "media-mover.service"],
        }
    ) == ["restic-offsite.service"]
