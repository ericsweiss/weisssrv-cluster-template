"""Coverage for generate-host-log-staleness.py.

The live tree is in step, so it proves nothing about failure: the derivation and
the --check arm run against fixture inventories, and each drift case must FAIL.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conftest import REPO, load_script

gen = load_script("generate-host-log-staleness.py")


SITE = textwrap.dedent(
    """\
    - name: Base
      hosts: base_managed:extra_servers:!deploy_skipped
      roles:
        - weisssrv.infra.base
        - weisssrv.infra.alloy_host
    """
)

HOSTS = textwrap.dedent(
    """\
    all:
      children:
        base_managed:
          children:
            proxmox:
              hosts:
                pve-one:
                pve-two:
        extra_servers:
          hosts:
            app-one:
    """
)


def write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path, gen.SITE_YML, SITE)
    write(tmp_path, gen.HOSTS_YML, HOSTS)
    write(tmp_path, gen.OUTPUT, gen.render(["pve-one", "pve-two", "app-one"]))
    return tmp_path


def test_hosts_come_out_in_inventory_order(repo: Path) -> None:
    assert gen.alloy_hosts(repo) == ["pve-one", "pve-two", "app-one"]


def test_a_host_in_two_groups_is_rendered_once(repo: Path) -> None:
    write(repo, gen.HOSTS_YML, HOSTS.replace("        app-one:", "        pve-one:"))
    assert gen.alloy_hosts(repo) == ["pve-one", "pve-two"]


def test_check_passes_on_a_generated_file(repo: Path) -> None:
    assert gen.main(["--repo-root", str(repo), "--check"]) == 0


def test_check_fails_when_the_inventory_gains_a_host(repo: Path) -> None:
    write(repo, gen.HOSTS_YML, HOSTS + "        app-two:\n")
    assert gen.main(["--repo-root", str(repo), "--check"]) == 1


def test_check_fails_when_a_rule_is_hand_deleted(repo: Path) -> None:
    body = (repo / gen.OUTPUT).read_text()
    write(repo, gen.OUTPUT, body.replace('host="app-one"', 'host="app-typo"'))
    assert gen.main(["--repo-root", str(repo), "--check"]) == 1


def test_a_hand_edited_header_is_drift(repo: Path) -> None:
    """The write path replaces the whole file, so a header edit survives only
    until the next sync. `--check` has to say so rather than call it clean."""
    body = (repo / gen.OUTPUT).read_text()
    write(repo, gen.OUTPUT, "# a local note\n" + body[body.index("groups:") :])
    assert gen.main(["--repo-root", str(repo), "--check"]) == 1


def test_regenerating_restores_the_generated_header(repo: Path) -> None:
    body = (repo / gen.OUTPUT).read_text()
    write(repo, gen.OUTPUT, "# a local note\n" + body[body.index("groups:") :])
    assert gen.main(["--repo-root", str(repo)]) == 0
    assert (repo / gen.OUTPUT).read_text().startswith(gen.HEADER)


def test_the_cluster_wide_arm_is_generated_too(repo: Path) -> None:
    """The per-host rules do not replace it: a host absent from the inventory
    is covered by nothing else."""
    assert "HostLogShippingStaleAll" in (repo / gen.OUTPUT).read_text()


def test_a_deleted_cluster_wide_arm_is_drift(repo: Path) -> None:
    body = (repo / gen.OUTPUT).read_text()
    write(repo, gen.OUTPUT, body.replace("HostLogShippingStaleAll", "HostLogShippingStaleNone"))
    assert gen.main(["--repo-root", str(repo), "--check"]) == 1


def test_writing_makes_check_pass(repo: Path) -> None:
    write(repo, gen.OUTPUT, "groups: []\n")
    assert gen.main(["--repo-root", str(repo)]) == 0
    assert gen.main(["--repo-root", str(repo), "--check"]) == 0


def test_count_is_the_rule_count(repo: Path, capsys) -> None:
    """Three hosts plus the cluster-wide arm. An operator convenience: nothing in
    the pipeline reads it, and no alert threshold is derived from it."""
    assert gen.main(["--repo-root", str(repo), "--count"]) == 0
    assert capsys.readouterr().out.strip() == "4"


def test_a_group_resolving_to_nothing_is_vacuous(repo: Path) -> None:
    write(
        repo,
        gen.HOSTS_YML,
        HOSTS.replace("    extra_servers:\n      hosts:\n        app-one:\n", ""),
    )
    with pytest.raises(gen.Vacuous):
        gen.alloy_hosts(repo)
    assert gen.main(["--repo-root", str(repo), "--check"]) == 2


def test_an_excluded_group_gets_no_rule(repo: Path) -> None:
    """A `!group` host never runs alloy_host, so a rule for it would fire forever."""
    write(repo, gen.HOSTS_YML, HOSTS + "    deploy_skipped:\n      hosts:\n        pve-two:\n")
    assert gen.alloy_hosts(repo) == ["pve-one", "app-one"]
    body = gen.render(gen.alloy_hosts(repo))
    assert 'host="pve-two"' not in body
    assert 'host="pve-one"' in body and 'host="app-one"' in body


def test_excluding_every_host_is_vacuous(repo: Path) -> None:
    write(repo, gen.SITE_YML, SITE.replace("!deploy_skipped", "!base_managed:!extra_servers"))
    with pytest.raises(gen.Vacuous):
        gen.alloy_hosts(repo)


def test_two_alloy_host_plays_are_vacuous(repo: Path) -> None:
    write(repo, gen.SITE_YML, SITE + SITE)
    with pytest.raises(gen.Vacuous):
        gen.alloy_hosts(repo)


def test_an_unparseable_site_yml_exits_2_naming_the_file(repo: Path, capsys) -> None:
    """A parse error must name site.yml, not "<unicode string>", and read as an
    operator error rather than as drift."""
    write(repo, gen.SITE_YML, "- hosts: [unclosed\n")
    with pytest.raises(gen.Vacuous):
        gen.alloy_hosts(repo)
    assert gen.main(["--repo-root", str(repo), "--check"]) == 2
    assert "site.yml" in capsys.readouterr().err


def test_an_unparseable_hosts_yml_exits_2_naming_the_file(repo: Path, capsys) -> None:
    write(repo, gen.HOSTS_YML, "all: [unclosed\n")
    with pytest.raises(gen.Vacuous):
        gen.alloy_hosts(repo)
    assert gen.main(["--repo-root", str(repo), "--check"]) == 2
    assert "hosts.yml" in capsys.readouterr().err


def test_the_shipped_file_carries_the_cluster_wide_arm() -> None:
    """A generated cluster ships the cluster-wide arm and fills the per-host
    rules with `task flux:sync-host-log-staleness`, the way scripts/hosts.env is
    filled — so `--check` is red until that first run, not vacuous."""
    shipped = REPO / gen.OUTPUT
    assert shipped.is_file(), f"the repository ships no {gen.OUTPUT} — this gate ran nothing"
    assert "HostLogShippingStaleAll" in shipped.read_text()
