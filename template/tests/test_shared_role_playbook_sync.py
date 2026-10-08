"""Hold the cross-cutting roles in step between site.yml and the stack playbooks.

site.yml applies node_exporter_host and alloy_host in plays of their own, and
each stack playbook re-applies them, so this asserts per host that the two agree.
"""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO

PLAYBOOKS = REPO / "ansible" / "playbooks"
INVENTORY_HOSTS = REPO / "ansible" / "inventories" / "prod" / "hosts.yml"
SITE = "site.yml"

# The roles the "Shared with site.yml" comments mark. A third cross-cutting role
# in site.yml belongs here and in every stack playbook it covers.
SHARED_ROLES = frozenset({"node_exporter_host", "alloy_host"})
MARKER = "Shared with site.yml"

needs_ansible = pytest.mark.skipif(
    not (PLAYBOOKS.is_dir() and INVENTORY_HOSTS.is_file()),
    reason="no ansible/playbooks or inventory in this repository",
)


def group_index(tree: dict) -> dict[str, set[str]]:
    """group name -> every host under it, children resolved."""
    direct: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}

    def walk(name: str, body) -> None:
        body = body or {}
        direct.setdefault(name, set()).update((body.get("hosts") or {}).keys())
        kids = body.get("children") or {}
        children.setdefault(name, set()).update(kids)
        for kid, kid_body in kids.items():
            walk(kid, kid_body)

    for name, body in (tree or {}).items():
        walk(name, body)

    def resolve(name: str, seen: set[str]) -> set[str]:
        if name in seen:
            return set()
        seen.add(name)
        found = set(direct.get(name, ()))
        for kid in children.get(name, ()):
            found |= resolve(kid, seen)
        return found

    return {name: resolve(name, set()) for name in set(direct) | set(children)}


def expand(pattern: str, groups: dict[str, set[str]]) -> set[str]:
    """A `a:b:c` host pattern as a set of hosts. A token naming no group is a host.

    `!group` tokens are dropped: the reachability ledger groups the deploy plays
    exclude exist only at runtime, so no inventory host can be one.
    """
    hosts: set[str] = set()
    for token in str(pattern).split(":"):
        token = token.strip()
        if token and not token.startswith("!"):
            hosts |= groups.get(token, {token})
    return hosts


def _plays(playbook: str) -> list[dict]:
    body = yaml.safe_load((PLAYBOOKS / playbook).read_text()) or []
    return [play for play in body if isinstance(play, dict) and "hosts" in play]


def _play_roles(play: dict) -> set[str]:
    """The role names a play applies, bare of their collection prefix."""
    names: set[str] = set()
    for entry in play.get("roles") or []:
        name = entry if isinstance(entry, str) else (entry or {}).get("role")
        if isinstance(name, str):
            names.add(name.rsplit(".", 1)[-1])
    return names


def shared_by_host(playbook: str, groups: dict[str, set[str]]) -> dict[str, set[str]]:
    """host -> the SHARED_ROLES that playbook applies to it."""
    result: dict[str, set[str]] = {}
    for play in _plays(playbook):
        shared = _play_roles(play) & SHARED_ROLES
        if not shared:
            continue
        for host in expand(play["hosts"], groups):
            result.setdefault(host, set()).update(shared)
    return result


def mismatches(playbook: str, groups: dict[str, set[str]]) -> list[str]:
    site = shared_by_host(SITE, groups)
    applied = shared_by_host(playbook, groups)
    covered: set[str] = set()
    for play in _plays(playbook):
        # A tasks-only helper play configures no host with a role.
        if not _play_roles(play):
            continue
        covered |= expand(play["hosts"], groups)
    return [
        f"{host}: {playbook} is missing {sorted(site.get(host, set()) - applied.get(host, set()))}, "
        f"declares unshared {sorted(applied.get(host, set()) - site.get(host, set()))}"
        for host in sorted(covered)
        if site.get(host, set()) != applied.get(host, set())
    ]


@pytest.fixture(scope="module")
def groups() -> dict[str, set[str]]:
    index = group_index(yaml.safe_load(INVENTORY_HOSTS.read_text()) or {})
    assert any(index.values()), f"{INVENTORY_HOSTS} resolved to no hosts"
    return index


def stack_playbooks() -> list[str]:
    """Playbooks, site.yml aside, whose plays apply a shared role."""
    if not PLAYBOOKS.is_dir():
        return []
    return sorted(
        path.name
        for path in PLAYBOOKS.glob("*.yml")
        if path.name != SITE
        and any(_play_roles(play) & SHARED_ROLES for play in _plays(path.name))
    )


@needs_ansible
def test_some_stack_playbook_reapplies_a_shared_role():
    """Every comparison below is vacuously true with no such playbook."""
    assert stack_playbooks(), (
        f"no playbook outside {SITE} applies {sorted(SHARED_ROLES)} — either the "
        "role names moved or this gate is examining nothing"
    )


@needs_ansible
@pytest.mark.parametrize("playbook", stack_playbooks())
def test_stack_playbook_matches_site_yml(playbook, groups):
    problems = mismatches(playbook, groups)
    assert not problems, (
        f"{playbook} and {SITE} disagree about the cross-cutting roles "
        f"{sorted(SHARED_ROLES)}:\n  "
        + "\n  ".join(problems)
        + f"\n\nAdd the role to {playbook} (or to {SITE}) so a standalone deploy "
        "and a full site run ship the same thing."
    )


@needs_ansible
def test_every_marked_playbook_applies_a_shared_role():
    """The marker comment is the signpost; a playbook keeping it after losing the
    role it marks sends the next reader to a play that no longer exists."""
    marked = {
        path.name
        for path in PLAYBOOKS.glob("*.yml")
        if path.name != SITE and MARKER in path.read_text(encoding="utf-8")
    }
    stale = sorted(marked - set(stack_playbooks()))
    assert not stale, (
        f"playbooks carrying {MARKER!r} but applying none of {sorted(SHARED_ROLES)}: "
        f"{stale} — drop the comment or restore the role."
    )


@needs_ansible
def test_the_comparison_fails_when_a_shared_role_is_dropped(groups):
    """Mutation case: a comparison of two empty sets would pass on every playbook."""
    playbook = stack_playbooks()[0]
    applied = shared_by_host(playbook, groups)
    site = shared_by_host(SITE, groups)
    assert applied, f"{playbook} declares no shared role — the parser stopped matching"
    dropped = {host: roles - {"alloy_host"} for host, roles in applied.items()}
    assert [host for host in dropped if site.get(host, set()) != dropped[host]], (
        f"dropping alloy_host from {playbook} produced no mismatch — the comparison "
        "is not actually comparing role sets"
    )


def test_a_role_less_side_play_does_not_widen_coverage(monkeypatch):
    """A tasks-only play (mail.yml re-seeds the cert key on the DNS primary) must
    not put its host in scope, while a host the playbook does configure still is.
    """
    index = group_index({"all": {"hosts": {"h1": None, "h2": None}}})
    plays = {
        SITE: [{"hosts": "h1:h2", "roles": ["weisssrv.infra.alloy_host"]}],
        "side.yml": [
            {"hosts": "h1", "roles": ["weisssrv.infra.alloy_host"]},
            {"hosts": "h2", "tasks": []},
        ],
        "bare.yml": [{"hosts": "h1", "roles": ["weisssrv.infra.base"]}],
    }
    monkeypatch.setitem(globals(), "_plays", lambda name: plays[name])
    assert mismatches("side.yml", index) == []
    assert len(mismatches("bare.yml", index)) == 1


def test_the_pattern_expander_unions_groups_and_hosts():
    """`a:b` is a union, and a token naming no group is a host name."""
    index = group_index(
        {
            "all": {
                "children": {
                    "parent": {"children": {"child": {"hosts": {"h1": None}}}},
                    "other": {"hosts": {"h2": None}},
                }
            }
        }
    )
    assert index["parent"] == {"h1"}
    assert expand("parent:other", index) == {"h1", "h2"}
    assert expand("parent:h3", index) == {"h1", "h3"}
