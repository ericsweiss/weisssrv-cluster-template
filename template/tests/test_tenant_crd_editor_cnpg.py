"""tenant-crd-editor must grant every namespaced CNPG kind a tenant applies.

The rules ship whether or not postgres_operator is on, so a cluster that adds
the operator later needs no RBAC change.
"""
from __future__ import annotations

import yaml
from conftest import REPO

WRITE_VERBS = {"get", "list", "watch", "create", "update", "patch", "delete", "deletecollection"}
# Must equal `kubectl api-resources --api-group=<group> --namespaced=true` for
# the pinned charts; re-check after a CNPG or plugin chart bump.
EXPECTED = {
    "postgresql.cnpg.io": {
        "backups", "clusters", "databaseroles", "databases", "failoverquorums",
        "imagecatalogs", "poolers", "publications", "scheduledbackups", "subscriptions",
    },
    "barmancloud.cnpg.io": {"objectstores"},
}


def _role() -> dict:
    found = sorted((REPO / "kubernetes" / "clusters").glob("*/tenants/tenant-crd-editor.yaml"))
    assert len(found) == 1, f"expected one tenant-crd-editor.yaml, found {found}"
    return yaml.safe_load(found[0].read_text())


def _rules_for(group: str) -> list[dict]:
    return [r for r in _role()["rules"] if r.get("apiGroups") == [group]]


def test_each_cnpg_group_has_exactly_one_rule():
    for group in EXPECTED:
        assert len(_rules_for(group)) == 1, group


def test_each_cnpg_rule_covers_the_namespaced_kinds_with_every_write_verb():
    for group, resources in EXPECTED.items():
        rule = _rules_for(group)[0]
        assert set(rule["resources"]) == resources, group
        assert set(rule["verbs"]) == WRITE_VERBS, group
