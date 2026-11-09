"""Behaviour of the generic playbooks under ansible/playbooks/. A Jinja
expression is evaluated through Ansible's own templar against the outputs the
real commands print, not matched as text."""

from __future__ import annotations

import pytest
import yaml
from conftest import REPO, ansible_templar, trust_as_template

PLAYBOOKS = REPO / "ansible" / "playbooks"
GATE = "playbook expressions"


def holds(expr, variables: dict | None = None) -> bool:
    """One `when:`, `failed_when:` or `that:` item as Ansible evaluates it."""
    templar = ansible_templar(variables or {}, GATE)
    return bool(templar.evaluate_conditional(trust_as_template(expr, GATE)))


def render(text, variables: dict | None = None):
    """`text` templated as a play templates it: a lone `{{ }}` keeps its native type."""
    templar = ansible_templar(variables or {}, GATE)
    return templar.template(trust_as_template(str(text), GATE))


def _load(rel: str):
    path = PLAYBOOKS / rel
    assert path.is_file(), f"{rel} is missing — this gate examined nothing"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _tasks(node):
    """Every task dict in a play, task list or block, at any depth."""
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        if not {"hosts", "import_playbook"} & node.keys():
            yield node
        for key in ("tasks", "pre_tasks", "post_tasks", "block", "rescue", "always"):
            yield from _tasks(node.get(key) or [])


def _play(rel: str, name_part: str) -> dict:
    plays = [p for p in _load(rel) if name_part in str(p.get("name", ""))]
    assert len(plays) == 1, f"{rel}: {len(plays)} plays named like {name_part!r}"
    return plays[0]


def _task(tasks, name_part: str) -> dict:
    found = [t for t in tasks if name_part in str(t.get("name", ""))]
    assert len(found) == 1, f"{len(found)} tasks named like {name_part!r}"
    return found[0]


def test_tailscale_upgrade_runs_through_the_role():
    """The role installs through its own dpkg hold; a bare apt task cannot."""
    tasks = list(_tasks(_play("maintenance/update-applications.yml", "Tailscale")))
    assert not {key for task in tasks for key in task} & {"ansible.builtin.apt", "apt"}
    includes = [t["ansible.builtin.include_role"]["name"] for t in tasks
                if "ansible.builtin.include_role" in t]
    assert includes == ["weisssrv.infra.tailscale"]


@pytest.mark.parametrize(
    "register", ["running_kernel", "installed_kernel_proxmox", "installed_kernel_debian"]
)
def test_read_only_kernel_probes_run_under_check(register):
    """Skipped under --check, the probes leave the kernel assert nothing to read."""
    tasks = [t for t in _tasks(_load("maintenance/_reboot-if-needed.yml"))
             if t.get("register") == register]
    assert len(tasks) == 1, f"{len(tasks)} tasks register {register}"
    assert tasks[0].get("check_mode") is False, f"{register} probe lacks check_mode: false"


def _nas_task(name_part: str) -> dict:
    return _task(list(_tasks(_play("postflight.yml", "Verify NAS storage"))), name_part)


@pytest.mark.parametrize(
    ("stdout", "rc", "fails"),
    [
        ("pool 'tank' is healthy", 0, False),
        ("  pool: tank\n state: ONLINE\nerrors: 2 data errors, use '-v' for a list", 0, True),
        ("cannot open 'tank': no such pool", 1, True),
    ],
)
def test_pool_health_needs_the_healthy_line(stdout, rc, fails):
    task = _nas_task("Check ZFS pool health")
    variables = {"zpool_health": {"stdout": stdout, "rc": rc}, "item": "tank"}
    assert holds(task["failed_when"], variables) is fails


@pytest.mark.parametrize(
    ("stdout", "rc", "fails"),
    [
        ("yes\ton", 0, False),
        ("no\toff", 0, False),
        ("-\t-", 0, False),
        ("no\ton", 0, True),
        ("", 1, True),
    ],
)
def test_dataset_mount_check_skips_canmount_off(stdout, rc, fails):
    task = _nas_task("Verify declared datasets are mounted")
    assert holds(task["failed_when"], {"dataset_mounted": {"stdout": stdout, "rc": rc}}) is fails


@pytest.mark.parametrize(
    ("lines", "rc", "fails"),
    [
        (["/export 10.0.0.0/24", "/export/appdata 10.0.0.0/24"], 0, False),
        (["/export 10.0.0.0/24"], 0, True),
        (["/export/appdata2 10.0.0.0/24", "/export x"], 0, True),
        (["/export 10.0.0.0/24", "/export/appdata 10.0.0.0/24"], 1, True),
    ],
)
def test_every_declared_export_must_be_active(lines, rc, fails):
    task = _nas_task("Check NFS exports are active")
    variables = {
        "nas_storage_exports": [{"path": "/export"}, {"path": "/export/appdata"}],
        "nfs_exports": {"rc": rc, "stdout_lines": ["Export list for localhost:", *lines]},
    }
    assert holds(task["failed_when"], variables) is fails


def test_the_plaintext_mount_scan_sees_mounts_by_address():
    play = _play("postflight.yml", "NFS-over-TLS on k3s nodes")
    task = _task(list(_tasks(play)), "Gather live NFS mounts from the NAS")
    assert "nas_address" in task["ansible.builtin.command"]["cmd"]
    assert "ansible_host" in play["vars"]["nas_address"]


@pytest.mark.parametrize(("guest_type", "cli"), [("qemu", "qm"), ("lxc", "pct")])
def test_the_vm_play_walks_the_appliances_with_the_cli_of_their_type(guest_type, cli):
    play = _play("proxmox-enable-autostart.yml", "for VMs")
    assert "services" in play["hosts"].split(":")
    tasks = list(_tasks(play))
    fact = _task(tasks, "Set the VM's Proxmox host")["ansible.builtin.set_fact"]
    entry = [{"vmid": 150, "node": "pve-a", "type": guest_type}]
    assert render(fact["vm_guest_cli"], {"pve_vm_entry": entry}) == cli
    for name in ("Read the current VM config", "Apply autostart and boot order"):
        assert str(_task(tasks, name)["ansible.builtin.command"]).startswith("{{ vm_guest_cli }} ")


def _reseed_hosts(rel: str, variables: dict) -> list:
    play = _play(rel, "Re-seed the cert-distribution key")
    includes = [t for t in _tasks(play) if "ansible.builtin.include_role" in t]
    assert len(includes) == 1, f"{rel}: the re-seed play includes {len(includes)} roles"
    assert includes[0]["ansible.builtin.include_role"] == {
        "name": "weisssrv.infra.acme_certs", "tasks_from": "reseed"}
    return list(render(includes[0]["vars"]["acme_certs_reseed_hosts"], variables))


TARGETS = [{"host": "relay"}, {"host": "nas"}, {"host": "legacy", "ssh_no_sudo": True},
           {"host": "opted-in", "ssh_no_sudo": False}]


def test_base_reseeds_every_sudo_target_the_probe_reached():
    assert _play("base.yml", "Re-seed").get("hosts") == "dns_primary:!deploy_skipped"
    variables = {"acme_certs_distribution_targets": TARGETS,
                 "groups": {"deploy_skipped": ["nas"]}}
    assert sorted(_reseed_hosts("base.yml", variables)) == ["opted-in", "relay"]


@pytest.mark.parametrize(
    ("targets", "want"),
    [(TARGETS, ["relay"]), (TARGETS[1:], [])],
    ids=["pinned", "unpinned"],
)
def test_mail_reseeds_the_relay_once_it_is_pinned(targets, want):
    variables = {"acme_certs_distribution_targets": targets,
                 "groups": {"mail": ["relay"]}}
    assert _reseed_hosts("mail.yml", variables) == want


REPLICATION_HOSTVARS = {
    "a": {"inventory_hostname": "a", "replication_probe": {"ping": "pong"}},
    "b": {"inventory_hostname": "b", "replication_probe": {"unreachable": True}},
    "c": {"inventory_hostname": "c", "replication_probe": {"unreachable": True}},
}


def _whens(node) -> list:
    when = node.get("when", [])
    return when if isinstance(when, list) else [when]


def _replication_report(name_part: str, variables: dict) -> bool:
    """Whether a replication post_task fires, its block's `when:` and its own
    read as Ansible does."""
    play = _play("proxmox-ha.yml", "storage replication")
    (block,) = play["post_tasks"]
    task = _task(block["block"], name_part)
    scope = {"ansible_play_hosts_all": list(REPLICATION_HOSTVARS),
             "hostvars": REPLICATION_HOSTVARS, **variables}
    for key, expr in play["vars"].items():
        scope[key] = render(expr, scope)
    return all(holds(c, scope) for c in _whens(block) + _whens(task))


def test_the_replication_play_names_every_node_it_could_not_reach():
    play = _play("proxmox-ha.yml", "storage replication")
    assert play.get("ignore_unreachable") is True
    missed = render(play["vars"]["replication_missed"],
                    {"ansible_play_hosts_all": list(REPLICATION_HOSTVARS),
                     "hostvars": REPLICATION_HOSTVARS})
    assert list(missed) == ["b", "c"]
    (block,) = play["post_tasks"]
    assert block.get("tags") == ["always"]
    assert "ansible.builtin.fail" in _task(block["block"], "Fail because")
    assert "task proxmox:ha" in _task(block["block"], "Fail because")["ansible.builtin.fail"]["msg"]


@pytest.mark.parametrize(("jobs", "enabled"), [([{"id": "100-0"}], True), ([], False)],
                         ids=["jobs", "no-jobs"])
def test_replication_probe_runs_only_with_jobs(jobs, enabled):
    variables = {"proxmox_ha_replication_jobs": jobs}
    assert _replication_report("Probe the connection", variables) is enabled


@pytest.mark.parametrize(
    ("jobs", "absent", "fails", "warns"),
    [([{"id": "100-0"}], [], True, False),
     ([{"id": "100-0"}], ["b"], True, True),
     ([{"id": "100-0"}], ["b", "c"], False, True),
     ([], [], False, False)],
    ids=["undeclared-fails", "one-declared", "all-declared-warns", "no-jobs-silent"],
)
def test_an_unreached_replication_node_fails_unless_declared_absent(jobs, absent, fails, warns):
    variables = {"proxmox_ha_replication_jobs": jobs, "deploy_expected_absent_hosts": absent}
    assert _replication_report("Fail because", variables) is fails
    assert _replication_report("declared-absent", variables) is warns


def test_zpool_failure_stops_before_missing_pools_are_computed():
    """An unloaded ZFS module empties the pool list; that must not read as no pools."""
    names = [t.get("name") for t in _tasks(_play("bootstrap/storage-bootstrap.yml",
                                                 "Detect existing storage"))]
    guard = names.index("Assert the ZFS tools answered")
    assert guard < names.index("Determine which pools are missing")
    task = _task(list(_tasks(_load("bootstrap/storage-bootstrap.yml"))), "ZFS tools answered")
    assert not holds(task["ansible.builtin.assert"]["that"][0], {"existing_pools": {"rc": 1}})


@pytest.mark.parametrize(
    ("state", "passes"),
    [({"status": {"ActiveState": "active"}}, True),
     ({"status": {"ActiveState": "failed"}}, False),
     ({}, False)],
)
def test_k3s_upgrade_preflight_stops_a_service_that_is_not_active(state, passes):
    task = _task(list(_tasks(_play("maintenance/update-k3s-nodes.yml", "Pre-flight"))),
                 "service is active before the upgrade")
    conditions = task["ansible.builtin.assert"]["that"]
    assert all(holds(c, {"k3s_service_state": state}) for c in conditions) is passes
