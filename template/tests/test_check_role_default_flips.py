"""Tests that check-role-default-flips.py catches a role default that changes unadopted."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import load_script

gate = load_script("check-role-default-flips.py")

HOSTS = textwrap.dedent(
    """\
    all:
      vars:
        site_wide_var: 1
      children:
        proxmox:
          vars:
            proxmox_group_var: 1
          hosts:
            pve-one:
              proxmox_self_reboot_safe: true
            pve-two:
        k3s:
          hosts:
            node-one:
    """
)


def _inventory(root: Path, hosts: str = HOSTS, **files: str) -> Path:
    inv = root / "prod"
    inv.mkdir(parents=True, exist_ok=True)
    (inv / "hosts.yml").write_text(hosts, encoding="utf-8")
    for rel, body in files.items():
        path = inv / rel.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return inv


def _playbooks(root: Path, **files: str) -> Path:
    books = root / "playbooks"
    books.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        path = books / rel.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    return books


def _site_play(roles, hosts: str = "all") -> str:
    """One play running each named collection role on `hosts`."""
    lines = [f"- hosts: {hosts}", "  roles:"]
    lines += [f"    - weisssrv.infra.{role}" for role in sorted(roles)]
    return "\n".join(lines) + "\n"


def _main_args(monkeypatch, tmp_path, old: dict, new: dict, playbook: str | None = None,
               deps: dict | None = None) -> list[str]:
    """Patch the lib side and lay out a fixture inventory and playbooks: main()'s argv."""
    inv = _inventory(tmp_path, **{"group_vars__all.yml": "unrelated_var: 1\n"})
    books = _playbooks(tmp_path, **{"site.yml": playbook or _site_play(set(old) | set(new))})
    monkeypatch.setattr(gate, "lib_root", lambda explicit=None, collection=None: tmp_path)
    monkeypatch.setattr(
        gate, "role_defaults", lambda lib, ref, collection=None: {"old": old, "new": new}[ref]
    )
    monkeypatch.setattr(gate, "role_dependencies", lambda lib, ref, collection=None: deps or {})
    return ["--from", "old", "--to", "new", "--inventory", str(inv), "--playbooks", str(books)]


def _run(monkeypatch, capsys, tmp_path, old: dict, new: dict, *extra: str,
         playbook: str | None = None, deps: dict | None = None) -> tuple[int, str]:
    """Drive main() over two in-memory default sets and fixture inventory and playbooks."""
    rc = gate.main([*_main_args(monkeypatch, tmp_path, old, new, playbook, deps), *extra])
    return rc, capsys.readouterr().err


@pytest.mark.parametrize(
    ("was", "now"),
    [
        (True, False),
        ("07:15", "08:00"),
        (9100, 9101),
        ({"a": "1"}, {"a": "2"}),
        (["restic-offsite.service"], []),
        (None, "nic1"),
        ("nic1", None),
        ({"a": 1}, "flat"),
        ("flat", {"a": 1}),
        ({"a": 1}, ["a"]),
        (1, True),
        (0, False),
        (1, 1.0),
    ],
)
def test_a_changed_value_or_type_is_a_flip(was, now) -> None:
    assert gate.flipped_defaults({"r": {"r_x": was}}, {"r": {"r_x": now}}) == [
        ("r", "r_x", was, now)
    ]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"r": {"r_x": {"a": "1"}}}, {"r": {"r_x": {"a": "1"}}}),
        ({"r": {"r_x": None}}, {"r": {"r_x": None}}),
        ({"r": {}}, {"r": {"r_new": True}}),
        ({}, {"brand_new": {"brand_new_enabled": True}}),
    ],
    ids=["unchanged-mapping", "unchanged-null", "new-key", "new-role"],
)
def test_an_unchanged_or_new_default_is_not_a_flip(old, new) -> None:
    assert gate.flipped_defaults(old, new) == []


def test_a_mapping_flip_names_its_changed_keys() -> None:
    text = gate.describe({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 20, "d": 4})
    assert text == "mapping changes at b, c, d"


@pytest.mark.parametrize(
    ("was", "now", "message"),
    [
        (None, "nic1", "r_x flips None -> 'nic1'"),
        ({"ll": "ls -l", "la": "ls -a"}, {"ll": "ls -lh", "la": "ls -a"}, "r_x mapping changes at ll"),
        ("07:15", "08:00", "r_x flips '07:15' -> '08:00'"),
        (["restic-offsite.service"], [], "r_x flips ['restic-offsite.service'] -> []"),
    ],
)
def test_an_undeclared_flip_fails_the_gate(monkeypatch, capsys, tmp_path, was, now, message) -> None:
    rc, err = _run(monkeypatch, capsys, tmp_path, {"r": {"r_x": was}}, {"r": {"r_x": now}})
    assert rc == 1
    assert message in err


def test_a_key_new_on_the_to_side_passes_the_gate(monkeypatch, capsys, tmp_path) -> None:
    rc, err = _run(monkeypatch, capsys, tmp_path, {"r": {}}, {"r": {"r_new": False}})
    assert rc == 0
    assert err == ""


def test_a_site_wide_declaration_passes_the_gate(monkeypatch, capsys, tmp_path) -> None:
    rc, err = _run(
        monkeypatch, capsys, tmp_path, {"r": {"unrelated_var": 1}}, {"r": {"unrelated_var": 2}}
    )
    assert rc == 0
    assert err == ""


def test_allow_passes_an_undeclared_flip(monkeypatch, capsys, tmp_path) -> None:
    rc, _ = _run(
        monkeypatch, capsys, tmp_path, {"r": {"r_x": 1}}, {"r": {"r_x": 2}}, "--allow", "r_x"
    )
    assert rc == 0


def test_vars_inline_in_hosts_yml_are_declared(tmp_path) -> None:
    keys = set(gate.inventory_declarations(_inventory(tmp_path)).scopes)
    assert {"site_wide_var", "proxmox_group_var", "proxmox_self_reboot_safe"} <= keys


def test_yaml_suffix_and_vars_directories_are_read(tmp_path) -> None:
    inv = _inventory(
        tmp_path,
        **{
            "group_vars__k3s.yaml": "k3s_labels: []\n",
            "group_vars__proxmox__10-net.yml": "proxmox_bridge: vmbr0\n",
            "host_vars__node-one.yaml": "node_only_var: 1\n",
        },
    )
    assert {"k3s_labels", "proxmox_bridge", "node_only_var"} <= set(gate.inventory_declarations(inv).scopes)


def test_a_site_wide_key_reaches_every_host(tmp_path) -> None:
    decl = gate.inventory_declarations(_inventory(tmp_path))
    assert decl.uncovered("site_wide_var") == []


def test_a_single_host_override_leaves_the_other_hosts_uncovered(tmp_path) -> None:
    inv = _inventory(tmp_path, **{"host_vars__pve-one.yml": "nic_tuning_guard: false\n"})
    decl = gate.inventory_declarations(inv)
    assert decl.uncovered("nic_tuning_guard") == ["node-one", "pve-two"]


def test_a_group_declaration_covers_its_hosts(tmp_path) -> None:
    decl = gate.inventory_declarations(_inventory(tmp_path))
    assert decl.uncovered("proxmox_group_var") == ["node-one"]


def test_a_partial_declaration_fails_the_gate(monkeypatch, capsys, tmp_path) -> None:
    rc, err = _run(
        monkeypatch,
        capsys,
        tmp_path,
        {"proxmox": {"proxmox_self_reboot_safe": False}},
        {"proxmox": {"proxmox_self_reboot_safe": True}},
    )
    assert rc == 1
    assert "some hosts only" in err
    assert "node-one, pve-two" in err


def test_allow_partial_passes_a_partial_declaration(monkeypatch, capsys, tmp_path) -> None:
    argv = _main_args(
        monkeypatch,
        tmp_path,
        {"proxmox": {"proxmox_self_reboot_safe": False}},
        {"proxmox": {"proxmox_self_reboot_safe": True}},
    )
    rc = gate.main([*argv, "--allow-partial"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "some hosts only" in captured.err
    assert "1 declared for some hosts only and accepted by --allow-partial" in captured.out


def test_allow_silences_a_partial_declaration(monkeypatch, capsys, tmp_path) -> None:
    rc, err = _run(
        monkeypatch,
        capsys,
        tmp_path,
        {"proxmox": {"proxmox_self_reboot_safe": False}},
        {"proxmox": {"proxmox_self_reboot_safe": True}},
        "--allow",
        "proxmox_self_reboot_safe",
    )
    assert rc == 0
    assert "some hosts only" not in err


CYCLIC = HOSTS + (
    "    loop_a:\n      children:\n        loop_b:\n          children:\n"
    "            loop_a:\n              hosts:\n                pve-one:\n"
)
HOSTS_ONLY = "all:\n  children:\n    proxmox:\n      hosts:\n        pve-one:\n"


@pytest.mark.parametrize(
    ("hosts", "files", "message"),
    [
        (CYCLIC, {}, "cycle"),
        (HOSTS_ONLY, {}, "declares no variables"),
        # group_vars alone name no hosts, so partial coverage would pass every flip.
        (None, {"group_vars__all.yml": "site_wide_var: 1\n"}, "no host index"),
        (HOSTS, {"group_vars__all.yml": "key: [unclosed\n"}, "group_vars"),
        ("all:\n  hosts:\n    - pve-one\n", {}, "not a mapping"),
        ("all:\n  children:\n    - proxmox\n", {}, "not a mapping"),
        ("all:\n  vars: site_wide\n  hosts:\n    pve-one:\n", {}, "not a mapping"),
        ("all:\n  hosts:\n    pve-one:\n      - a_var\n", {}, "not a mapping"),
    ],
    ids=["group-cycle", "hosts-only", "no-host-index", "unparseable-file", "hosts-list",
         "children-list", "vars-scalar", "host-vars-list"],
)
def test_an_unusable_inventory_is_vacuous(tmp_path, hosts, files, message) -> None:
    inv = _inventory(tmp_path, hosts=hosts or "", **files)
    if hosts is None:
        (inv / "hosts.yml").unlink()
    with pytest.raises(gate.OperatorError, match=message):
        set(gate.inventory_declarations(inv).scopes)


def test_an_empty_inventory_is_vacuous(tmp_path) -> None:
    with pytest.raises(gate.OperatorError, match="declares no variables"):
        gate.inventory_declarations(tmp_path)


def test_group_hosts_split_across_inventory_files_are_merged(tmp_path) -> None:
    hosts = "all:\n  children:\n    proxmox:\n      hosts:\n        pve-one:\n"
    hosts += "    k3s:\n      hosts:\n        node-one:\n"
    inv = _inventory(
        tmp_path,
        hosts=hosts,
        **{
            "more.yml": "all:\n  children:\n    proxmox:\n      hosts:\n        pve-two:\n",
            "group_vars__proxmox.yml": "proxmox_key: 1\n",
        },
    )
    decl = gate.inventory_declarations(inv)
    assert decl.all_hosts() == {"pve-one", "pve-two", "node-one"}
    assert decl.uncovered("proxmox_key") == ["node-one"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=gate@example.invalid",
         "-c", "user.name=gate", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
         *args],
        check=True,
        capture_output=True,
    )


def _defaults(lib: Path, role: str, body: str, name: str = "main.yml") -> None:
    path = lib / gate.COLLECTION / "roles" / role / "defaults" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    """A git checkout with two tagged collection states."""
    root = tmp_path / "lib"
    root.mkdir()
    _git(root, "init", "-q")
    _defaults(root, "alpha", "alpha_port: 1\nalpha_on: true\n")
    _defaults(root, "beta", "beta_name: a\n", name="main.yaml")
    (root / gate.COLLECTION / "roles" / "nodefaults" / "tasks").mkdir(parents=True)
    (root / gate.COLLECTION / "roles" / "nodefaults" / "tasks" / "main.yml").write_text("---\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    _git(root, "tag", "v1")
    _defaults(root, "alpha", "alpha_port: 2\nalpha_on: true\n")
    _defaults(root, "beta", "beta_name: b\n", name="main.yaml")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "two")
    _git(root, "tag", "v2")
    return root


def test_role_defaults_reads_every_role_at_a_ref(lib: Path) -> None:
    assert gate.role_defaults(lib, "v1") == {
        "alpha": {"alpha_port": 1, "alpha_on": True},
        "beta": {"beta_name": "a"},
    }


def _meta(lib: Path, body: str) -> None:
    meta = lib / gate.COLLECTION / "roles" / "alpha" / "meta" / "main.yml"
    meta.parent.mkdir(parents=True)
    meta.write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("change", "ref", "read", "message"),
    [
        (None, "v9", "role_defaults", "git ls-tree"),
        (lambda lib: _defaults(lib, "alpha", "alpha_port: [unclosed\n"), "HEAD", "role_defaults",
         "defaults/main.yml"),
        (lambda lib: _defaults(lib, "alpha", "- alpha_port\n- alpha_on\n"), "HEAD",
         "role_defaults", "not a mapping"),
        (lambda lib: _git(lib, "rm", "-rq", f"{gate.COLLECTION}/roles/alpha/defaults",
                          f"{gate.COLLECTION}/roles/beta/defaults"), "HEAD", "role_defaults",
         "ships no role defaults"),
        (lambda lib: _meta(lib, "dependencies:\n  beta: {}\n"), "HEAD", "role_dependencies",
         "not a list"),
    ],
    ids=["unknown-ref", "unparseable-defaults", "defaults-not-mapping", "no-role-defaults",
         "meta-dependencies-not-list"],
)
def test_an_unusable_lib_ref_is_vacuous(lib: Path, change, ref, read, message) -> None:
    if change:
        change(lib)
        _git(lib, "add", "-A")
        _git(lib, "commit", "-qm", "change")
    with pytest.raises(gate.OperatorError, match=message):
        getattr(gate, read)(lib, ref)


def test_main_yml_wins_over_main_yaml(lib: Path) -> None:
    _defaults(lib, "beta", "beta_name: from-yml\n")
    _git(lib, "add", "-A")
    _git(lib, "commit", "-qm", "both")
    assert gate.role_defaults(lib, "HEAD")["beta"] == {"beta_name": "from-yml"}


def test_main_reports_flips_from_a_real_checkout(lib: Path, tmp_path: Path, capsys) -> None:
    inv = _inventory(tmp_path, **{"group_vars__all.yml": "alpha_port: 2\n"})
    books = _playbooks(tmp_path, **{"site.yml": _site_play({"alpha", "beta"})})
    rc = gate.main(["--from", "v1", "--to", "v2", "--lib", str(lib), "--inventory", str(inv),
                    "--playbooks", str(books)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "beta_name" in err
    assert "alpha_port" not in err


def test_the_lib_path_comes_from_the_environment(monkeypatch, tmp_path) -> None:
    (tmp_path / gate.COLLECTION).mkdir(parents=True)
    monkeypatch.setenv("WEISSSRV_LIB_PATH", str(tmp_path))
    assert gate.lib_root() == tmp_path


def test_an_explicit_lib_path_wins_over_the_environment(monkeypatch, tmp_path) -> None:
    explicit = tmp_path / "explicit"
    (explicit / gate.COLLECTION).mkdir(parents=True)
    monkeypatch.setenv("WEISSSRV_LIB_PATH", str(tmp_path / "absent"))
    assert gate.lib_root(str(explicit)) == explicit


# Partial coverage is scoped to the hosts the flipped role runs on.

K3S_ONLY = (
    {"k3s": {"k3s_api_vip": "10.0.0.1"}},
    {"k3s": {"k3s_api_vip": "10.0.0.2"}},
)


def test_a_group_declaration_for_a_group_scoped_role_is_not_partial(
    monkeypatch, capsys, tmp_path
) -> None:
    _inventory(tmp_path, **{"group_vars__k3s.yml": "k3s_api_vip: 10.0.0.9\n"})
    rc, err = _run(monkeypatch, capsys, tmp_path, *K3S_ONLY, playbook=_site_play({"k3s"}, "k3s"))
    assert rc == 0, err
    assert err == ""


def test_the_same_declaration_is_partial_for_a_role_run_everywhere(
    monkeypatch, capsys, tmp_path
) -> None:
    """Mutation proof: only the play's host scope makes the case above pass."""
    _inventory(tmp_path, **{"group_vars__k3s.yml": "k3s_api_vip: 10.0.0.9\n"})
    rc, err = _run(monkeypatch, capsys, tmp_path, *K3S_ONLY, playbook=_site_play({"k3s"}))
    assert rc == 1
    assert "pve-one, pve-two" in err


def test_a_single_host_override_among_the_role_hosts_is_partial(
    monkeypatch, capsys, tmp_path
) -> None:
    rc, err = _run(
        monkeypatch,
        capsys,
        tmp_path,
        {"proxmox": {"proxmox_self_reboot_safe": False}},
        {"proxmox": {"proxmox_self_reboot_safe": True}},
        playbook=_site_play({"proxmox"}, "proxmox"),
    )
    assert rc == 1
    assert "no declaration reaches pve-two" in err
    assert "node-one" not in err


def test_a_role_no_play_runs_is_not_partial(monkeypatch, capsys, tmp_path) -> None:
    rc, err = _run(
        monkeypatch,
        capsys,
        tmp_path,
        {"proxmox": {"proxmox_self_reboot_safe": False}},
        {"proxmox": {"proxmox_self_reboot_safe": True}},
        playbook=_site_play({"other"}),
    )
    assert rc == 0, err


def test_a_meta_dependency_runs_where_its_dependant_runs(monkeypatch, capsys, tmp_path) -> None:
    rc, err = _run(
        monkeypatch,
        capsys,
        tmp_path,
        {"proxmox": {"proxmox_self_reboot_safe": False}},
        {"proxmox": {"proxmox_self_reboot_safe": True}},
        playbook=_site_play({"parent"}),
        deps={"parent": {"proxmox"}},
    )
    assert rc == 1
    assert "node-one, pve-two" in err


def _targets(tmp_path, hosts: str = HOSTS, **books: str) -> tuple[dict, object]:
    decl = gate.inventory_declarations(_inventory(tmp_path, hosts=hosts))
    return gate.role_targets(_playbooks(tmp_path, **books), decl), decl


def test_pattern_terms_that_narrow_a_play_are_dropped(tmp_path) -> None:
    decl = gate.inventory_declarations(_inventory(tmp_path))
    assert decl.pattern_hosts("proxmox:!k3s") == {"pve-one", "pve-two"}
    assert decl.pattern_hosts("proxmox:&k3s") == {"pve-one", "pve-two"}
    assert decl.pattern_hosts("proxmox[0]") == {"pve-one", "pve-two"}
    assert decl.pattern_hosts("node-one,localhost") == {"node-one", "localhost"}
    assert decl.pattern_hosts(["k3s", "pve-two"]) == {"node-one", "pve-two"}


def test_an_unreadable_pattern_has_no_static_hosts(tmp_path) -> None:
    decl = gate.inventory_declarations(_inventory(tmp_path))
    assert decl.pattern_hosts("{{ target }}") is None
    assert decl.pattern_hosts("no_such_group") is None


def test_a_role_behind_an_unreadable_pattern_targets_every_host(tmp_path) -> None:
    targets, decl = _targets(tmp_path, **{"site.yml": _site_play({"alpha"}, "'{{ target }}'")})
    assert targets["alpha"] == decl.all_hosts()


def test_include_role_through_a_task_file_takes_the_play_hosts(tmp_path) -> None:
    targets, decl = _targets(
        tmp_path,
        **{
            "site.yml": """\
                - hosts: k3s
                  tasks:
                    - ansible.builtin.include_tasks: tasks/inner.yml
                """,
            "tasks__inner.yml": """\
                - block:
                    - ansible.builtin.include_role:
                        name: weisssrv.infra.alpha
                      vars:
                        alpha_target: x
                """,
        },
    )
    assert targets == {"alpha": {"node-one"}}
    assert decl.play_cover["alpha_target"] == {"alpha": {"node-one"}}


def test_a_task_file_no_play_reaches_targets_every_host(tmp_path) -> None:
    targets, decl = _targets(
        tmp_path,
        **{
            "site.yml": _site_play({"alpha"}, "k3s"),
            "tasks__orphan.yml": "- ansible.builtin.import_role:\n    name: weisssrv.infra.alpha\n",
        },
    )
    assert targets["alpha"] == decl.all_hosts()


def test_a_templated_include_widens_every_task_file_role(tmp_path) -> None:
    targets, decl = _targets(
        tmp_path,
        **{
            "site.yml": """\
                - hosts: k3s
                  tasks:
                    - ansible.builtin.include_tasks: tasks/inner.yml
                    - ansible.builtin.include_tasks: "{{ extra }}.yml"
                """,
            "tasks__inner.yml": "- ansible.builtin.include_role:\n    name: weisssrv.infra.alpha\n",
        },
    )
    assert targets["alpha"] == decl.all_hosts()


def test_play_vars_files_cover_the_play_hosts(tmp_path) -> None:
    """A localhost play loading host vars explicitly declares them for itself."""
    targets, decl = _targets(
        tmp_path,
        **{
            "vars__ha.yml": "ha_host_key: abc\n",
            "ha.yml": """\
                - hosts: localhost
                  vars:
                    ha_inline: 1
                  vars_files:
                    - vars/ha.yml
                  roles:
                    - role: weisssrv.infra.home_assistant
                      vars:
                        ha_role_var: 1
                """,
        },
    )
    assert targets == {"home_assistant": {"localhost"}}
    for key in ("ha_host_key", "ha_inline", "ha_role_var"):
        assert decl.uncovered(key, targets["home_assistant"]) == []
    assert decl.uncovered("ha_other", targets["home_assistant"]) == ["localhost"]


def test_a_play_declared_key_is_not_unadopted(tmp_path) -> None:
    _, decl = _targets(
        tmp_path, **{"site.yml": "- hosts: k3s\n  vars:\n    alpha_x: 1\n"
                                 "  roles: [weisssrv.infra.alpha]\n"}
    )
    assert gate.unadopted([("alpha", "alpha_x", 1, 2)], decl, set()) == []


def test_play_vars_cover_only_the_roles_that_play_runs(tmp_path) -> None:
    targets, decl = _targets(
        tmp_path,
        **{"site.yml": "- hosts: k3s\n  vars:\n    shared_x: 1\n  roles: [weisssrv.infra.alpha]\n"
                       "- hosts: k3s\n  roles: [weisssrv.infra.beta]\n"},
    )
    assert gate.unadopted([("beta", "shared_x", 1, 2)], decl, set())
    assert decl.uncovered("shared_x", targets["alpha"], "alpha") == []


def test_a_role_entry_var_covers_only_that_role(tmp_path) -> None:
    """A var passed to one role must not adopt another role's flip."""
    targets, decl = _targets(
        tmp_path,
        **{"site.yml": """\
            - hosts: k3s
              roles:
                - role: weisssrv.infra.alpha
                  vars:
                    shared_x: 1
                - weisssrv.infra.beta
              tasks:
                - ansible.builtin.include_role:
                    name: weisssrv.infra.gamma
                  vars:
                    gamma_y: 1
            """},
    )
    assert targets == {"alpha": {"node-one"}, "beta": {"node-one"}, "gamma": {"node-one"}}
    flips = [("alpha", "shared_x", 1, 2), ("beta", "shared_x", 1, 2),
             ("gamma", "gamma_y", 1, 2), ("beta", "gamma_y", 1, 2)]
    notes = gate.unadopted(flips, decl, set())
    assert [n.split(":")[0] for n in notes] == ["beta", "beta"]
    assert decl.uncovered("shared_x", targets["beta"], "beta") == ["node-one"]


def test_play_vars_cover_the_meta_dependencies_of_its_roles(tmp_path) -> None:
    decl = gate.inventory_declarations(_inventory(tmp_path))
    books = _playbooks(tmp_path, **{
        "site.yml": "- hosts: k3s\n  vars:\n    leaf_x: 1\n  roles: [weisssrv.infra.top]\n"})
    targets = gate.role_targets(books, decl, dependencies={"top": {"leaf"}})
    assert targets["leaf"] == {"node-one"}
    assert decl.uncovered("leaf_x", targets["leaf"], "leaf") == []


def test_a_narrowed_play_covers_no_host(tmp_path) -> None:
    """`proxmox:!pve-two` runs the role widely but its vars cover nowhere."""
    targets, decl = _targets(
        tmp_path,
        **{"site.yml": "- hosts: 'proxmox:!pve-two'\n  vars:\n    alpha_x: 1\n"
                       "  roles: [weisssrv.infra.alpha]\n"},
    )
    assert targets["alpha"] == {"pve-one", "pve-two"}
    assert gate.unadopted([("alpha", "alpha_x", 1, 2)], decl, set())
    assert gate.narrows("proxmox:&k3s") and gate.narrows(["proxmox", "!k3s"])
    assert not gate.narrows("proxmox,k3s")


def test_an_undecodable_playbook_exits_two(monkeypatch, capsys, tmp_path) -> None:
    args = _main_args(monkeypatch, tmp_path, {"alpha": {"alpha_x": 1}}, {"alpha": {"alpha_x": 2}})
    (tmp_path / "playbooks" / "broken.yml").write_bytes(b"- hosts: all\n  vars: {x: \xff\xfe}\n")
    assert gate.main(args) == 2
    assert "broken.yml" in capsys.readouterr().err


def test_meta_dependencies_spread_transitively() -> None:
    targets = {"top": {"a", "b"}, "mid": {"c"}}
    gate._spread_dependencies(targets, {"top": {"mid"}, "mid": {"leaf"}})
    assert targets["mid"] == {"a", "b", "c"}
    assert targets["leaf"] == {"a", "b", "c"}


def test_a_playbooks_dir_without_plays_is_vacuous(tmp_path) -> None:
    decl = gate.inventory_declarations(_inventory(tmp_path))
    with pytest.raises(gate.OperatorError, match="holds no plays"):
        gate.role_targets(_playbooks(tmp_path, **{"tasks__t.yml": "- debug: {}\n"}), decl)
    with pytest.raises(gate.OperatorError, match="not a playbooks directory"):
        gate.role_targets(tmp_path / "absent", decl)


def test_role_dependencies_reads_meta_at_a_ref(lib: Path) -> None:
    meta = lib / gate.COLLECTION / "roles" / "alpha" / "meta" / "main.yml"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        "dependencies:\n  - weisssrv.infra.beta\n  - role: gamma\n  - name: '{{ x }}'\n",
        encoding="utf-8",
    )
    _git(lib, "add", "-A")
    _git(lib, "commit", "-qm", "meta")
    assert gate.role_dependencies(lib, "HEAD") == {"alpha": {"beta", "gamma"}}


def test_a_defaults_directory_is_read_and_wins_over_main_yml(lib: Path) -> None:
    _defaults(lib, "alpha", "alpha_port: 7\n", name="main/10-ports.yml")
    _defaults(lib, "alpha", "alpha_port: 8\nalpha_dir: true\n", name="main/20-more.yaml")
    _defaults(lib, "delta", "delta_on: false\n", name="main/sub/a.yml")
    _git(lib, "add", "-A")
    _git(lib, "commit", "-qm", "split defaults")
    roles = gate.role_defaults(lib, "HEAD")
    assert roles["alpha"] == {"alpha_port": 8, "alpha_dir": True}
    assert roles["delta"] == {"delta_on": False}


def test_a_removed_default_is_a_flip() -> None:
    flips = gate.flipped_defaults({"r": {"r_x": 1, "r_y": 2}}, {"r": {"r_y": 2}})
    assert flips == [("r", "r_x", 1, gate.REMOVED)]


def test_an_undeclared_removed_default_fails_the_gate(monkeypatch, capsys, tmp_path) -> None:
    rc, err = _run(monkeypatch, capsys, tmp_path, {"r": {"r_x": 1, "r_y": 2}}, {"r": {"r_y": 2}})
    assert rc == 1
    assert "r_x is removed from the role defaults" in err


def test_a_declared_removed_default_passes_the_gate(monkeypatch, capsys, tmp_path) -> None:
    rc, err = _run(
        monkeypatch, capsys, tmp_path, {"r": {"unrelated_var": 1}}, {"r": {"r_other": 2}}
    )
    assert rc == 0, err


def test_a_removed_default_read_from_a_real_checkout_is_a_flip(lib: Path) -> None:
    _defaults(lib, "alpha", "alpha_port: 2\n")
    _git(lib, "commit", "-qam", "drop alpha_on")
    flips = gate.flipped_defaults(gate.role_defaults(lib, "v2"), gate.role_defaults(lib, "HEAD"))
    assert flips == [("alpha", "alpha_on", True, gate.REMOVED)]


@pytest.mark.parametrize(
    ("collection", "env", "argv", "message"),
    [
        (False, None, ["--lib", "{tmp}"], "carries no"),
        (False, "{tmp}", [], "carries no"),
        (True, None, ["--lib", "{tmp}"], "could not run"),
    ],
    ids=["lib-without-collection", "env-lib-without-collection", "no-git-on-path"],
)
def test_an_unusable_lib_path_exits_two(
    monkeypatch, capsys, tmp_path, collection, env, argv, message
) -> None:
    if collection:
        (tmp_path / gate.COLLECTION).mkdir(parents=True)
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    if env:
        monkeypatch.setenv("WEISSSRV_LIB_PATH", env.format(tmp=tmp_path))
    argv = [arg.format(tmp=tmp_path) for arg in argv]
    assert gate.main(["--from", "v1", "--to", "v2", *argv]) == 2
    assert message in capsys.readouterr().err
