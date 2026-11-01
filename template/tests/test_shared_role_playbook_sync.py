"""Hold the cross-cutting roles in step between site.yml and the stack playbooks.

site.yml and each stack playbook apply node_exporter_host and alloy_host; this
asserts per host that the two agree, parsing with check-deploy-host-coverage.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO, load_script

coverage = load_script("check-deploy-host-coverage.py")

PLAYBOOKS = REPO / "ansible" / "playbooks"
SITE = "site.yml"

# The roles the "Shared with site.yml" comments mark. A third cross-cutting role
# in site.yml belongs here and in every stack playbook it covers.
SHARED_ROLES = frozenset({"node_exporter_host", "alloy_host"})
MARKER = "Shared with site.yml"


def _shared(roles) -> set[str]:
    return {role.rsplit(".", 1)[-1] for role in roles} & SHARED_ROLES


def shared_by_host(playbook: str, groups: dict[str, set[str]], root: Path = REPO) -> dict[str, set[str]]:
    """host -> the SHARED_ROLES that playbook applies to it."""
    result: dict[str, set[str]] = {}
    for play in coverage.parse_playbook(playbook, root):
        shared = _shared(play["roles"])
        if not shared:
            continue
        for host in coverage.expand(play["hosts"], groups):
            result.setdefault(host, set()).update(shared)
    return result


def mismatches(playbook: str, groups: dict[str, set[str]], root: Path = REPO) -> list[str]:
    """Hosts the playbook configures whose shared roles differ from site.yml's.
    Scope is the plays that apply a role themselves: a helper play that only
    includes a task file, like the cert re-seed, puts no host in scope."""
    site = shared_by_host(SITE, groups, root)
    applied = shared_by_host(playbook, groups, root)
    covered: set[str] = set()
    for play in coverage.parse_playbook(playbook, root, follow_task_files=False):
        covered |= coverage.expand(play["hosts"], groups)
    return [
        f"{host}: {playbook} is missing {sorted(site.get(host, set()) - applied.get(host, set()))}, "
        f"declares unshared {sorted(applied.get(host, set()) - site.get(host, set()))}"
        for host in sorted(covered)
        if site.get(host, set()) != applied.get(host, set())
    ]


def _playbook_files() -> list[Path]:
    """Every playbook directly under ansible/playbooks/, either YAML suffix."""
    return sorted(p for suffix in ("yml", "yaml") for p in PLAYBOOKS.glob(f"*.{suffix}"))


def stack_playbooks() -> list[str]:
    """Playbooks, site.yml aside, whose plays apply a shared role."""
    return sorted(
        path.name
        for path in _playbook_files()
        if path.name != SITE
        and any(_shared(play["roles"]) for play in coverage.parse_playbook(path.name, REPO))
    )


@pytest.fixture(scope="module")
def groups() -> dict[str, set[str]]:
    index = coverage.build_inventory(REPO)
    assert index.get("all"), "the inventory resolved to no hosts"
    return index


def test_some_stack_playbook_reapplies_a_shared_role():
    """Every comparison below is vacuously true with no such playbook."""
    assert stack_playbooks(), (
        f"no playbook outside {SITE} applies {sorted(SHARED_ROLES)} — either the "
        "role names moved or this gate is examining nothing"
    )


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


def test_every_marked_playbook_applies_a_shared_role():
    """The marker comment is the signpost; a playbook keeping it after losing the
    role it marks sends the next reader to a play that no longer exists."""
    marked = {
        path.name
        for path in _playbook_files()
        if path.name != SITE and MARKER in path.read_text(encoding="utf-8")
    }
    stale = sorted(marked - set(stack_playbooks()))
    assert not stale, (
        f"playbooks carrying {MARKER!r} but applying none of {sorted(SHARED_ROLES)}: "
        f"{stale} — drop the comment or restore the role."
    )


def test_the_comparison_fails_when_a_shared_role_is_dropped(groups):
    """Mutation case: a comparison of two empty sets would pass on every playbook."""
    playbooks = stack_playbooks()
    assert playbooks, "no stack playbook to mutate"
    applied = shared_by_host(playbooks[0], groups)
    site = shared_by_host(SITE, groups)
    assert applied, f"{playbooks[0]} declares no shared role — the parser stopped matching"
    dropped = {host: roles - {"alloy_host"} for host, roles in applied.items()}
    assert [host for host in dropped if site.get(host, set()) != dropped[host]], (
        f"dropping alloy_host from {playbooks[0]} produced no mismatch — the comparison "
        "is not actually comparing role sets"
    )


def _fixture_repo(root: Path, playbooks: dict[str, str]) -> dict[str, set[str]]:
    inventory = root / coverage.HOSTS_YML
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all:\n  hosts:\n    h1:\n    h2:\n")
    book_dir = root / "ansible" / "playbooks"
    book_dir.mkdir(parents=True)
    for name, body in playbooks.items():
        (book_dir / name).write_text(body)
    return coverage.build_inventory(root)


def test_a_role_less_side_play_does_not_widen_coverage(tmp_path):
    """A tasks-only play must not put its host in scope, while a host the
    playbook configures with a role still is."""
    index = _fixture_repo(
        tmp_path,
        {
            SITE: "- hosts: h1:h2\n  roles: [weisssrv.infra.alloy_host]\n",
            "side.yml": (
                "- hosts: h1\n  roles: [weisssrv.infra.alloy_host]\n"
                "- hosts: h2\n  tasks: []\n"
            ),
            "bare.yml": "- hosts: h1\n  roles: [weisssrv.infra.base]\n",
        },
    )
    assert mismatches("side.yml", index, tmp_path) == []
    assert len(mismatches("bare.yml", index, tmp_path)) == 1


def test_a_play_that_only_includes_a_task_file_does_not_widen_coverage(tmp_path):
    """The re-seed shape: a role reached through a task file is still declared
    for the deploy gate, but its play configures no host for this comparison."""
    index = _fixture_repo(
        tmp_path,
        {
            SITE: "- hosts: h1:h2\n  roles: [weisssrv.infra.alloy_host]\n",
            "side.yml": (
                "- hosts: h1\n  roles: [weisssrv.infra.alloy_host]\n"
                "- hosts: h2\n  tasks:\n    - ansible.builtin.include_tasks: tasks/_helper.yml\n"
            ),
        },
    )
    (tmp_path / "ansible" / "playbooks" / "tasks").mkdir()
    (tmp_path / "ansible" / "playbooks" / "tasks" / "_helper.yml").write_text(
        "- ansible.builtin.include_role:\n    name: weisssrv.infra.acme_certs\n"
    )
    assert mismatches("side.yml", index, tmp_path) == []
    declared = [p["hosts"] for p in coverage.parse_playbook("side.yml", tmp_path)]
    assert declared == ["h1", "h2"], "the deploy gate stopped following the task file"
