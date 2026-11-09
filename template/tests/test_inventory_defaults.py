"""No inventory variable may restate, or misspell, a role variable.

A value equal to a role default is a restatement; a `<role>_` key the pinned
collection never reads is inert. The guest prefix length follows cluster-config.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import pytest
import yaml
from conftest import (
    CONFIGMAP_INVENTORY_MIRROR,
    LIB_CHECKOUT,
    REPO,
    REQUIREMENTS,
    disk_role_defaults,
    lib_git,
    load_script,
    pinned_source,
    role_defaults,
)

INVENTORY = REPO / "ansible" / "inventories" / "prod"
CLUSTER_CONFIG = REPO / "kubernetes/infrastructure/sources/cluster-config.yaml"
ROLES_RELPATH = f"{load_script('check-role-default-flips.py').COLLECTION}/roles"
# Both guest-provisioning roles default to /24; only the inventory knows better.
PREFIX_VARS = ("proxmox_lxc_netmask_bits", "proxmox_vm_cloudinit_prefix_len")


# Variables rendered straight from a copier answer. The inventory spells them
# out because the answer chooses them, so equalling the role default is a
# property of that answer rather than a restatement.
_ANSWER_DRIVEN = {
    "proxmox_lxc_netmask_bits",
    "proxmox_vm_cloudinit_prefix_len",
    "tailscale_accept_dns",
    "tailscale_accept_routes",
    "tailscale_advertise_routes",
    "tailscale_enabled",
}

# Opt-in role flags the validate-rendered-cluster role-opt-ins check requires the
# inventory to spell out: a role invoked unconditionally whose flag is set
# nowhere skips every task and still reports success.
_REQUIRED_BY_OPT_IN_GATE = {
    "vfio_passthrough_enabled",
}

# Boot-ordering knobs the setup guide sends the operator to the inventory for.
# The declaration is where the operator finds the knob, so it stays.
_DOCUMENTED_IN_SETUP = {
    "nas_storage_encrypted_bind_sources",
}

# (inventory file relative to ansible/inventories/prod, variable) pairs where the
# starter inventory restates the role default. Debt, not configuration: delete the
# inventory line and the entry together.
ACKNOWLEDGED: set[tuple[str, str]] = {
    ("group_vars/dns.yml", "acme_certs_ssh_user"),
    ("group_vars/dns.yml", "adguard_home_dhcp_enabled"),
    ("group_vars/dns.yml", "adguard_home_dns_port"),
    ("group_vars/dns.yml", "adguard_home_doq_port"),
    ("group_vars/dns.yml", "adguard_home_dot_port"),
    ("group_vars/dns.yml", "adguard_home_enable_dnssec"),
    ("group_vars/dns.yml", "adguard_home_fallback_dns"),
    ("group_vars/dns.yml", "adguard_home_http_port"),
    ("group_vars/dns.yml", "adguard_home_install_path"),
    ("group_vars/dns.yml", "adguard_home_protection_enabled"),
    ("group_vars/dns.yml", "adguard_home_tls_enabled"),
    ("group_vars/dns.yml", "unbound_port"),
    ("group_vars/k3s.yml", "k3s_etcd_snapshot_nfs_export"),
    ("group_vars/k3s.yml", "k3s_flannel_backend"),
    ("group_vars/k3s.yml", "k3s_registry_host_pins"),
    ("group_vars/mail.yml", "smtp_relay_upstream"),
    ("group_vars/nas.yml", "nas_storage_archive_backup_enabled"),
    ("group_vars/nas.yml", "nas_storage_smartd_enabled"),
    ("group_vars/nas.yml", "nas_storage_smartd_nvme_disks"),
    ("group_vars/nas.yml", "nas_storage_smartd_ssd_disks"),
    ("group_vars/nas.yml", "nas_storage_smartd_tank_disks"),
    ("group_vars/nas.yml", "nas_storage_zfs_scrub_enabled"),
    ("group_vars/nas.yml", "proxmox_backup_storage"),
    ("group_vars/nas.yml", "proxmox_backup_vzdump_jobs"),
    ("group_vars/proxmox.yml", "proxmox_firewall_egress_filtering"),
    ("group_vars/proxmox.yml", "proxmox_ha_replication_jobs"),
    ("group_vars/proxmox.yml", "proxmox_ha_resources"),
    ("group_vars/proxmox.yml", "proxmox_ha_rules"),
}

# (inventory file, variable) pairs whose prefix collides with a role name but
# which are not role variables at all.
NOT_ROLE_VARS: set[tuple[str, str]] = {
    # In-cluster Helm chart pin; the `gitlab` role prefix is a collision.
    ("group_vars/all.yml", "gitlab_runner_helm_version"),
    # Delegate target for the maintenance playbooks' cluster-wide kubectl; the
    # `k3s` role prefix is a collision.
    ("group_vars/k3s.yml", "k3s_delegate_server"),
}

needs_inventory = pytest.mark.skipif(
    not (INVENTORY / "group_vars").is_dir() or not REQUIREMENTS.is_file(),
    reason="no ansible/inventories/prod or requirements.yml",
)


def _load(name: str):
    return load_script(name)


@functools.lru_cache(maxsize=1)
def role_names() -> tuple[str, ...]:
    """The pinned collection's role directory names, longest first so the most
    specific prefix of a variable wins."""
    kind, source = pinned_source()
    if kind == "disk":
        names = [path.name for path in Path(source).iterdir() if path.is_dir()]
    else:
        code, listing = lib_git("ls-tree", "--name-only", f"{source}:{ROLES_RELPATH}")
        assert code == 0, f"could not list the roles of {source} in {LIB_CHECKOUT}"
        names = [line.rstrip("/") for line in listing.split()]
    assert names, "the pinned collection lists no roles"
    return tuple(sorted(names, key=len, reverse=True))


def mentioned_in(candidates: set[str], roles: Path) -> set[str]:
    """Which of `candidates` a roles tree on disk names anywhere.

    Defaults alone are too narrow: plenty of role variables are only read in
    tasks or templates, with no entry in defaults/main.yml.
    """
    if not candidates:
        return set()
    pattern = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, candidates))) + r")\b")
    found: set[str] = set()
    for path in roles.rglob("*"):
        if path.is_file():
            found.update(pattern.findall(path.read_text(errors="replace")))
    return found


def collection_mentions(candidates: set[str]) -> set[str]:
    """Which of `candidates` the pinned collection names anywhere in its roles."""
    if not candidates:
        return set()
    kind, source = pinned_source()
    if kind == "disk":
        return mentioned_in(candidates, Path(source))
    alternation = "|".join(sorted(map(re.escape, candidates)))
    code, out = lib_git("grep", "-h", "-o", "-w", "-E", alternation, source, "--", ROLES_RELPATH)
    assert code in (0, 1), f"git grep failed against {source} in {LIB_CHECKOUT}"
    return {token for token in re.findall(r"[A-Za-z0-9_]+", out) if token in candidates}


def _inventory_files(inventory: Path) -> list[Path]:
    """Every vars file in the inventory, both YAML suffixes. The emptiness
    assert is a floor: a moved tree would otherwise make every check pass."""
    paths = sorted(
        path
        for sub in ("group_vars", "host_vars")
        for suffix in ("*.yml", "*.yaml")
        for path in (inventory / sub).rglob(suffix)
    )
    assert paths, f"no vars files under {inventory}/group_vars or host_vars"
    return paths


def role_prefixed_keys(inventory: Path, names: tuple[str, ...]) -> list[tuple[str, str]]:
    """(file, variable) for every inventory key that starts with `<role>_`."""
    found: list[tuple[str, str]] = []
    for path in _inventory_files(inventory):
        site = yaml.safe_load(path.read_text()) or {}
        if not isinstance(site, dict):
            continue
        rel = str(path.relative_to(inventory))
        for key in site:
            if any(str(key).startswith(f"{role}_") for role in names):
                found.append((rel, str(key)))
    return found


def derived_exemptions() -> set[str]:
    """Variables another gate requires the inventory to spell out."""
    registry = _load("version-registry.py")
    tracked: set[str] = set(registry.CONFIG.get("untracked_allowlist") or [])
    for service in registry.CONFIG.get("services") or []:
        tracked.add(str(service["var_name"]).split(".")[0])
        tracked.update(service.get("coupled_vars") or [])
        if service.get("checksum_var"):
            tracked.add(service["checksum_var"])
    mirrors = {var_name for _file, var_name in CONFIGMAP_INVENTORY_MIRROR.values()}
    return tracked | mirrors | _ANSWER_DRIVEN | _REQUIRED_BY_OPT_IN_GATE | _DOCUMENTED_IN_SETUP


def restatements(inventory: Path, defaults, exempt: set[str]) -> list[tuple[str, str, str]]:
    """(file, variable, role) for every inventory value equal to a role default."""
    found = []
    for path in _inventory_files(inventory):
        site = yaml.safe_load(path.read_text()) or {}
        if not isinstance(site, dict):
            continue
        for key, value in site.items():
            if key in exempt:
                continue
            for role, default in defaults.get(key, []):
                if default == value:
                    found.append((str(path.relative_to(inventory)), key, role))
                    break
    return found


@pytest.fixture(scope="module")
def found() -> list[tuple[str, str, str]]:
    return restatements(INVENTORY, role_defaults(), derived_exemptions())


@needs_inventory
def test_no_new_variable_restates_its_role_default(found):
    new = sorted((f, k) for f, k, _role in found if (f, k) not in ACKNOWLEDGED)
    assert not new, (
        f"inventory variables that only restate the role default: {new} — delete "
        "them; the role already supplies the value, and a pinned copy silently "
        "outlives the next default change."
    )


def _declared(inventory: Path) -> set[tuple[str, str]]:
    """(file, variable) for everything the inventory actually declares."""
    declared = set()
    for path in _inventory_files(inventory):
        site = yaml.safe_load(path.read_text()) or {}
        if isinstance(site, dict):
            rel = str(path.relative_to(inventory))
            declared |= {(rel, key) for key in site}
    return declared


@needs_inventory
def test_acknowledged_entries_still_restate(found):
    """A stale ACKNOWLEDGED entry fails: only a variable this inventory still
    restates belongs in the exemption list.
    """
    live = {(f, k) for f, k, _role in found}
    stale = sorted((ACKNOWLEDGED & _declared(INVENTORY)) - live)
    assert not stale, (
        f"these no longer match their role default: {stale} — drop them from "
        "ACKNOWLEDGED; the list is debt, not configuration."
    )


def test_the_collector_reports_a_restatement(tmp_path):
    """Mutation case: the comparison has to fire on a copied default."""
    inventory = tmp_path / "inv"
    (inventory / "group_vars").mkdir(parents=True)
    (inventory / "host_vars").mkdir(parents=True)
    (inventory / "group_vars" / "all.yml").write_text("demo_port: 8080\ndemo_mode: lax\n")
    defaults = {"demo_port": [("demo", 8080)], "demo_mode": [("demo", "strict")]}
    assert restatements(inventory, defaults, set()) == [
        ("group_vars/all.yml", "demo_port", "demo")
    ]
    assert restatements(inventory, defaults, {"demo_port"}) == []


def test_a_yaml_suffixed_vars_file_is_read(tmp_path):
    """Mutation case: a `.yaml` vars file is inventory too."""
    inventory = tmp_path / "inv"
    (inventory / "host_vars").mkdir(parents=True)
    (inventory / "host_vars" / "node.yaml").write_text("demo_port: 8080\n")
    defaults = {"demo_port": [("demo", 8080)]}
    assert restatements(inventory, defaults, set()) == [
        ("host_vars/node.yaml", "demo_port", "demo")
    ]


def test_an_inventory_with_no_vars_files_is_vacuous(tmp_path):
    with pytest.raises(AssertionError, match="no vars files"):
        restatements(tmp_path, {}, set())


def lan_prefix_length() -> int:
    """The prefix length of cluster-config's cluster_lan_cidr."""
    data = (yaml.safe_load(CLUSTER_CONFIG.read_text()) or {}).get("data") or {}
    cidr = str(data.get("cluster_lan_cidr", ""))
    assert "/" in cidr, f"{CLUSTER_CONFIG} declares no cluster_lan_cidr — nothing to pin to"
    return int(cidr.split("/")[1])


@needs_inventory
@pytest.mark.skipif(not CLUSTER_CONFIG.is_file(), reason="no cluster-config ConfigMap")
def test_the_guest_prefix_length_follows_the_lan_cidr():
    """A guest built with the role's /24 on a LAN that is not one black-holes
    every off-subnet address it treats as on-link."""
    site = yaml.safe_load((INVENTORY / "group_vars" / "all.yml").read_text()) or {}
    expected = lan_prefix_length()
    missing = [name for name in PREFIX_VARS if name not in site]
    assert not missing, (
        f"group_vars/all.yml does not set {missing} — the guest roles then build "
        "every LXC and VM with their own /24 default."
    )
    wrong = {name: site[name] for name in PREFIX_VARS if int(site[name]) != expected}
    assert not wrong, (
        f"group_vars/all.yml disagrees with cluster_lan_cidr (/{expected}): {wrong}"
    )


@pytest.fixture(scope="module")
def prefixed() -> list[tuple[str, str]]:
    return role_prefixed_keys(INVENTORY, role_names())


@needs_inventory
def test_no_inventory_variable_is_unknown_to_the_pinned_collection(prefixed):
    """A `<role>_` variable the collection never reads is silently inert: the
    role's `| default(...)` guard takes the default and nothing fails."""
    known = collection_mentions({key for _file, key in prefixed})
    unknown = sorted(
        pair for pair in prefixed if pair[1] not in known and pair not in NOT_ROLE_VARS
    )
    assert not unknown, (
        f"inventory variables the pinned collection never reads: {unknown} — fix "
        "the spelling, or add the pair to NOT_ROLE_VARS if the prefix is a "
        "collision."
    )


def test_a_misspelled_role_variable_is_unknown(tmp_path):
    """Mutation case: a one-character typo must not read as a real role variable,
    and an unprefixed key must not enter the corpus at all."""
    inventory = tmp_path / "inv"
    (inventory / "group_vars").mkdir(parents=True)
    (inventory / "host_vars").mkdir(parents=True)
    (inventory / "group_vars" / "all.yml").write_text(
        "demo_port: 1\ndemo_porrt: 1\nunprefixed_key: 1\n"
    )
    found = role_prefixed_keys(inventory, ("demo",))
    assert ("group_vars/all.yml", "demo_porrt") in found
    assert ("group_vars/all.yml", "unprefixed_key") not in found

    roles = tmp_path / "roles" / "demo" / "tasks"
    roles.mkdir(parents=True)
    (roles / "main.yml").write_text("- debug: var=demo_port\n")
    known = mentioned_in({key for _file, key in found}, tmp_path / "roles")
    assert known == {"demo_port"}


def test_a_split_defaults_directory_wins_over_main_yml(tmp_path):
    """Ansible reads defaults/main/ before main.yml, and main.yml before
    main.yaml, so a role that splits its defaults is still in the corpus."""
    roles = tmp_path / "roles"
    (roles / "split" / "defaults" / "main").mkdir(parents=True)
    (roles / "split" / "defaults" / "main" / "a.yml").write_text("split_port: 1\n")
    (roles / "split" / "defaults" / "main" / "b.yaml").write_text("split_port: 2\n")
    (roles / "split" / "defaults" / "main.yml").write_text("split_ignored: 1\n")
    (roles / "both" / "defaults").mkdir(parents=True)
    (roles / "both" / "defaults" / "main.yml").write_text("both_port: yml\n")
    (roles / "both" / "defaults" / "main.yaml").write_text("both_port: yaml\n")
    (roles / "bare" / "tasks").mkdir(parents=True)
    assert disk_role_defaults(roles) == {
        "both_port": [("both", "yml")],
        "split_port": [("split", 2)],
    }


@pytest.mark.parametrize("ci, outcome", [("1", pytest.fail.Exception), ("", pytest.skip.Exception)])
def test_a_missing_collection_fails_under_ci_and_skips_locally(tmp_path, monkeypatch, ci, outcome):
    """With no installed collection and no checkout, the role-defaults gates would
    pass empty; CI must red rather than skip."""
    import conftest

    monkeypatch.setenv("CI", ci)
    monkeypatch.setattr(conftest, "REPO", tmp_path / "repo")
    monkeypatch.setattr(conftest, "LIB_CHECKOUT", tmp_path / "no-checkout")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    conftest.pinned_source.cache_clear()
    try:
        with pytest.raises(outcome, match="no library checkout"):
            conftest.pinned_source()
    finally:
        conftest.pinned_source.cache_clear()
