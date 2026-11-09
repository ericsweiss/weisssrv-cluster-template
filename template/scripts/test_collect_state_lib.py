"""Tests for scripts/collect-state.sh: its source guards, the backup section's
coverage, probe parity across both modes and the probes' failure handling.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


# source guard: the dead-fallback idiom must not come back
# `producer | head -N || echo "msg"` (or tail/wc) can never print msg — the
# pipeline exits with head's status. This is the gate on collect-state.sh.

COLLECT_STATE = Path(__file__).resolve().parent / "collect-state.sh"


def test_the_collector_ships_executable():
    # The Taskfile execs ./scripts/collect-state.sh directly.
    assert COLLECT_STATE.is_file(), "the collector is missing"
    assert os.access(COLLECT_STATE, os.X_OK), f"{COLLECT_STATE.name} is not executable"


def _code(lines: list[str]) -> str:
    """Drop whole-line comments so a probe named only in prose does not count."""
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


DEAD_FALLBACK_RE = re.compile(
    r"\|\s*(head|tail|wc)(\s[^|\n]*)?\|\|\s*echo"
)

# Both pin the behaviour, not the call shape: a `timeout` with no duration and a
# status test against anything but 124 are what a loosened pin lets through.
SNAPSHOT_TIMEOUT_RE = re.compile(r"\btimeout\s+\d+[smhd]?\s+\S")
SNAPSHOT_RC124_RE = re.compile(r'\$rc"?\s*\]?\s*(-eq|==)\s*124\b')


class TestNoDeadPipelineFallbacks:
    def test_collect_state_has_no_head_tail_wc_or_echo(self):
        offenders = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(COLLECT_STATE.read_text().splitlines(), 1)
            if DEAD_FALLBACK_RE.search(line)
        ]
        assert not offenders, (
            "`producer | head/tail/wc ... || echo MSG` can never print MSG "
            "(the pipeline exits with head's status), so a probe failure "
            "renders as an empty section. Use cs_emit / cs_capped instead:\n"
            + "\n".join(offenders)
        )

    def test_the_guard_actually_matches_the_broken_idiom(self):
        # A gate that cannot fail proves nothing: pin the pattern to the broken
        # shapes it must catch.
        for broken in (
            'systemctl --failed --no-legend | head -20 || echo "none"',
            "sudo postqueue -p 2>/dev/null | tail -1 || echo 'Cannot check'",
            'kubectl get cm --no-headers | wc -l || echo "0"',
        ):
            assert DEAD_FALLBACK_RE.search(broken), broken
        for ok in (
            'systemctl --failed --no-legend | cs_emit "none"',
            'zfs list | cs_capped 50 "No ZFS"',
            'sudo pct list 2>/dev/null || echo "No LXC containers"',
        ):
            assert not DEAD_FALLBACK_RE.search(ok), ok


# DR coverage of the NAS backup section. CLUSTER_STATUS.txt is the artifact a
# restore is planned from, so these pin the sections whose absence would be
# invisible in the output.

class TestBackupSectionCoverage:
    SRC = _code(COLLECT_STATE.read_text().splitlines())

    @pytest.mark.parametrize(
        "prom",
        [
            "archive_backup.prom",
            "restic_offsite.prom",
            "restic_offsite_verify.prom",
            "backup_artifact_mtime.prom",
            "backup_restore_drill.prom",
            "pve_cluster_backup.prom",
            "vzdump_backup.prom",
        ],
    )
    def test_every_backup_textfile_is_collected(self, prom):
        assert f"cat /var/lib/node_exporter/{prom}" in self.SRC, (
            f"{prom} is produced on the NAS but never collected"
        )

    def test_restic_snapshot_inventory_is_listed_and_bounded(self):
        assert "restic-offsitectl snapshots" in self.SRC, (
            "retention states the INTENT; only the snapshot list states the real "
            "recovery depth (the offsite backup runbook)"
        )
        line = next(
            ln for ln in self.SRC.splitlines() if "restic-offsitectl snapshots" in ln
        )
        assert SNAPSHOT_TIMEOUT_RE.search(line), (
            "the listing reaches the bucket, so the call needs `timeout <duration>` "
            "with a real bound; the bare word is not a timeout"
        )
        # The producer is captured and its status tested, so cs_capped bounds the
        # output on the SUCCESS branch a few lines below rather than on this one.
        block = self._snapshot_block()
        assert "cs_capped" in block, "the snapshot listing must go through cs_capped"

    def test_a_failed_snapshot_listing_does_not_read_as_an_empty_repository(self):
        """A fallback describes the empty case only, never a failed listing."""
        block = self._snapshot_block()
        assert "rc=$?" in block, "the producer's exit status must be captured, not discarded"
        assert SNAPSHOT_RC124_RE.search(block), (
            "the timeout branch (rc 124) must be tested separately"
        )
        assert "NO snapshots" in block, "the empty-repository case must say so explicitly"

    @classmethod
    def _snapshot_block(cls) -> str:
        return cls.SRC.split("restic-offsitectl snapshots", 1)[1].split("\nfi\n", 1)[0]

    @pytest.mark.parametrize(
        "broken",
        [
            "snaps=$(sudo timeout restic-offsitectl snapshots 2>&1); rc=$?",
            'snaps=$(sudo timeout "$SNAP_TIMEOUT" restic-offsitectl snapshots); rc=$?',
        ],
    )
    def test_the_timeout_pin_rejects_a_dropped_duration(self, broken):
        """Mutation case: a loosened pin would accept the bare word `timeout`."""
        assert not SNAPSHOT_TIMEOUT_RE.search(broken), broken
        assert SNAPSHOT_TIMEOUT_RE.search(
            "snaps=$(sudo timeout 90 restic-offsitectl snapshots 2>&1); rc=$?"
        )

    def test_the_rc_pin_rejects_a_status_test_against_another_code(self):
        """Mutation case: rc 1240 or rc 12 must not satisfy the timeout branch."""
        assert not SNAPSHOT_RC124_RE.search('if [ "$rc" -eq 1240 ]; then')
        assert not SNAPSHOT_RC124_RE.search('if [ "$rc" -eq 12 ]; then')
        assert SNAPSHOT_RC124_RE.search('if [ "$rc" -eq 124 ]; then')


def require_jq() -> None:
    """Fail rather than skip: a silent skip is how the jq-backed probes lose
    their only coverage in a job that stopped installing jq."""
    assert shutil.which("jq"), (
        "jq is not installed, so the jq-backed probes are unverified. "
        "Install jq in the job that runs this suite."
    )


# probe parity: every shared probe must run in BOTH collect-state.sh modes
# Every probe_* must run in both collect-state.sh modes.

PROBE_DEF_RE = re.compile(r"^(probe_[a-z0-9_]+)\(\)", re.MULTILINE)
JSON_BRANCH_START = 'if [ "${1:-}" = "--json" ]; then'


def _split_modes() -> tuple[str, str]:
    """Return (json_branch_code, regular_mode_code) from collect-state.sh."""
    lines = COLLECT_STATE.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == JSON_BRANCH_START)
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi")
    return _code(lines[start + 1:end]), _code(lines[end + 1:])


class TestProbeParity:
    def test_every_probe_is_called_in_both_modes(self):
        json_branch, regular = _split_modes()
        probes = PROBE_DEF_RE.findall(COLLECT_STATE.read_text())
        assert probes, "no probe_* definitions found — the parser drifted"
        missing = [
            p for p in probes
            if p not in json_branch or p not in regular
        ]
        assert not missing, (
            "these probes run in only one mode, so the two classifiers no "
            f"longer see the same signals: {missing}"
        )

    def test_the_split_finds_both_halves(self):
        # A gate that cannot fail proves nothing: each half must hold its own
        # classifier and not the other's.
        json_branch, regular = _split_modes()
        assert "classify_json" in json_branch
        assert "classify_regular" not in json_branch
        assert "classify_regular" in regular


# probe_zfs_degraded: a pool that failed to import has no `zpool list` row at
# all, so absence on a storage host must count, not read as zero degraded.

def _extract_function(name: str) -> str:
    """The shell text of one top-level function in collect-state.sh."""
    src = COLLECT_STATE.read_text().splitlines()
    start = next(i for i, ln in enumerate(src) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(src)) if src[i] == "}")
    return "\n".join(src[start:end + 1])


def _probe_zfs(listings: dict[str, str | int | None], nas_hosts: str,
               expect: str = "", unknown: bool = False,
               detail: bool = False) -> tuple[str, ...]:
    """Drive probe_zfs_degraded over stubbed `zpool list` output per host: None
    is ssh failing, an int a failed zpool's rc. Returns count, missing and,
    with `detail`, the pool list.
    """
    arms = []
    for host, listing in listings.items():
        if listing is None:
            body = "return 255"
        elif isinstance(listing, int):
            body = f"return {listing}"
        else:
            body = f"printf '%s' {shlex.quote(listing)}"
        arms.append(f"    {host}) {body} ;;")
    script = "\n".join([
        "set -euo pipefail",
        "SSH_USER=ops",
        "PVE_HOSTS=(" + " ".join(shlex.quote(h) for h in listings) + ")",
        f"NAS_HOSTS={shlex.quote(nas_hosts)}",
        f"NAS_ZFS_POOLS={shlex.quote(expect)}",
        f"NAS_ZFS_POOLS_UNKNOWN={'true' if unknown else 'false'}",
        "ssh_probe_cmd() {",
        '  case "${1#*@}" in',
        *arms,
        "    *) return 255 ;;",
        "  esac",
        "}",
        _extract_function("probe_zfs_degraded"),
        "probe_zfs_degraded detail" if detail else "probe_zfs_degraded",
        'echo "$ZFS_DEGRADED_RESULT"',
        'echo "$ZFS_MISSING_RESULT"',
        'echo "$ZFS_POOLS_RESULT" | jq -c .' if detail else "",
    ])
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    out = res.stdout.split("\n")
    return (out[0], out[1], out[2]) if detail else (out[0], out[1])


class TestProbeZfsDegraded:
    def test_every_expected_pool_present_is_clean(self):
        count, missing = _probe_zfs(
            {"nas-01": "tank\tONLINE\nfast\tONLINE\n"},
            nas_hosts="nas-01", expect="tank fast",
        )
        assert (count, missing) == ("0", "")

    def test_a_pool_missing_from_the_listing_counts(self):
        count, missing = _probe_zfs(
            {"nas-01": "tank\tONLINE\n"},
            nas_hosts="nas-01", expect="tank fast",
        )
        assert count == "1"
        assert missing == "pool fast is NOT IMPORTED on nas-01"

    def test_a_storage_host_with_no_pool_row_counts_without_a_roster(self):
        count, missing = _probe_zfs({"nas-01": ""}, nas_hosts="nas-01")
        assert count == "1"
        assert missing == "no pool is imported on nas-01"

    def test_an_unreachable_storage_host_names_its_expected_pools(self):
        count, missing = _probe_zfs({"nas-01": None}, nas_hosts="nas-01", expect="tank")
        assert count == "1"
        assert missing == "pool tank could not be listed on nas-01"

    @pytest.mark.parametrize("rc", [124, 255])
    def test_an_unreachable_non_storage_host_is_left_to_the_reachability_count(self, rc):
        assert _probe_zfs({"opt-01": rc}, nas_hosts="nas-01") == ("0", "")

    @pytest.mark.parametrize("host", ["opt-01", "nas-01"])
    def test_a_failed_zpool_list_on_an_answering_host_counts(self, host):
        """A ZFS module that did not load must not read as zero degraded pools."""
        count, missing = _probe_zfs({host: 1}, nas_hosts="nas-01", expect="tank")
        assert (count, missing) == ("1", f"zpool list failed on {host}")

    def test_a_non_storage_host_with_no_pools_is_not_a_finding(self):
        assert _probe_zfs({"opt-01": ""}, nas_hosts="nas-01") == ("0", "")

    def test_a_degraded_pool_still_counts(self):
        count, missing = _probe_zfs(
            {"nas-01": "tank\tDEGRADED\n"}, nas_hosts="nas-01", expect="tank",
        )
        assert (count, missing) == ("1", "")

    def test_the_detail_pool_list_covers_every_host(self):
        """--json's zfs.pools must agree with degraded_count, which spans all hosts."""
        require_jq()
        row = "\t1T\t1G\t999G\n"
        count, _, pools = _probe_zfs(
            {"nas-01": f"tank\tONLINE{row}", "opt-01": f"rpool\tDEGRADED{row}",
             "opt-02": None},
            nas_hosts="nas-01", expect="tank", detail=True,
        )
        assert count == "1"
        assert [(p["host"], p["name"], p["health"]) for p in json.loads(pools)] == [
            ("nas-01", "tank", "ONLINE"), ("opt-01", "rpool", "DEGRADED"),
        ]


# The NAS_ZFS_POOLS roster the absence check is gated on: derived from
# nas_storage_zfs_pools at run time, so a renamed pool is still probed.


def _derive_nas_pools(nas_yml: str | None, end_marker) -> tuple[str, str, str]:
    """(roster, NAS_ZFS_POOLS_UNKNOWN, stderr) from collect-state.sh's lines
    starting at the NAS_ZFS_POOLS assignment and ending at `end_marker(line)`."""
    lines = COLLECT_STATE.read_text().splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith("NAS_ZFS_POOLS=")), None
    )
    assert start is not None, "the NAS_ZFS_POOLS derivation is gone from collect-state.sh"
    end = next(i for i in range(start, len(lines)) if end_marker(lines[i]))
    block = "\n".join(lines[start:end + 1])
    with tempfile.TemporaryDirectory() as tmp:
        scripts = Path(tmp) / "scripts"
        scripts.mkdir()
        if nas_yml is not None:
            group_vars = Path(tmp) / "ansible/inventories/prod/group_vars"
            group_vars.mkdir(parents=True)
            (group_vars / "nas.yml").write_text(nas_yml)
        script = "\n".join([
            "set -euo pipefail",
            f"_SCRIPT_DIR={shlex.quote(str(scripts))}",
            block,
            'printf "%s\\n%s" "$NAS_ZFS_POOLS" "${NAS_ZFS_POOLS_UNKNOWN:-}"',
        ])
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    roster, unknown = res.stdout.split("\n")[:2]
    return roster, unknown, res.stderr


def _derive_nas_pools_verdict(nas_yml: str | None) -> tuple[str, str, str]:
    """The derivation plus its unknown-roster guard."""
    return _derive_nas_pools(nas_yml, lambda line: line == "fi")


def _derive_nas_zfs_pools(nas_yml: str | None) -> str:
    """The roster alone, from the NAS_ZFS_POOLS assignment."""
    return _derive_nas_pools(nas_yml, lambda line: "nas.yml" in line)[0]


class TestNasZfsPoolRoster:
    STUB = textwrap.dedent("""\
        nas_storage_zfs_pools:
          - name: tank
            datasets:
              - name: tank/media
          - name: fast
            datasets:
              - name: fast/apps
        nas_nfs_exports:
          - name: media
        """)

    def test_the_roster_is_the_inventory_pool_names(self):
        assert _derive_nas_zfs_pools(self.STUB).split() == ["tank", "fast"]

    def test_a_renamed_pool_is_reported_not_imported(self):
        # Without the roster the absence check examines nothing: the weaker arm
        # only fires when a storage host has ZERO pools imported.
        count, missing = _probe_zfs(
            {"nas-01": "tank\tONLINE\n"},
            nas_hosts="nas-01", expect=_derive_nas_zfs_pools(self.STUB),
        )
        assert count == "1"
        assert missing == "pool fast is NOT IMPORTED on nas-01"

    def test_a_missing_inventory_leaves_the_roster_empty(self):
        assert _derive_nas_zfs_pools(None) == ""


class TestNasZfsPoolRosterUnknown:
    """An unreadable roster must read as unknown, not as "nothing expected"."""

    def test_a_missing_inventory_is_recorded_unknown_and_warns(self):
        roster, unknown, stderr = _derive_nas_pools_verdict(None)
        assert roster.split() == []
        assert unknown == "true"
        assert "nas.yml" in stderr

    def test_a_readable_roster_is_not_unknown(self):
        roster, unknown, stderr = _derive_nas_pools_verdict(
            TestNasZfsPoolRoster.STUB
        )
        assert roster.split() == ["tank", "fast"]
        assert (unknown, stderr) == ("false", "")

    def test_an_unknown_roster_counts_instead_of_passing(self):
        count, missing = _probe_zfs(
            {"nas-01": "tank\tONLINE\n"}, nas_hosts="nas-01", unknown=True,
        )
        assert count == "1"
        assert "could not read the expected pool list" in missing


# probe_flux_not_ready: a query that never answered must read as unknown, so the
# caller's coerce_int demotes the verdict instead of promoting a false zero.

def _probe_flux(payload: str | None) -> str:
    """Drive probe_flux_not_ready over a stubbed kubectl. None = query failed."""
    body = "return 1" if payload is None else f"printf '%s' {shlex.quote(payload)}"
    script = "\n".join([
        "set -euo pipefail",
        f"kubectl() {{ {body}; }}",
        _extract_function("probe_flux_not_ready"),
        "probe_flux_not_ready",
    ])
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


class TestProbeFluxNotReady:
    @pytest.fixture(autouse=True)
    def _jq_present(self):
        require_jq()

    def test_a_failed_query_is_unknown_not_zero(self):
        assert _probe_flux(None) == "unknown"

    def test_unparseable_json_is_unknown_not_zero(self):
        assert _probe_flux("not json at all") == "unknown"

    def test_all_ready_is_zero(self):
        assert _probe_flux(json.dumps({"items": [
            {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]})) == "0"

    def test_a_suspended_object_counts(self):
        assert _probe_flux(json.dumps({"items": [
            {"spec": {"suspend": True},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]})) == "1"


class TestFluxCallSitesCoerce:
    """Both modes must feed the classifiers a coerced number: an "unknown"
    reaching classify_json's `-eq 0` test is a bash error, not a verdict."""

    SRC = _code(COLLECT_STATE.read_text().splitlines())

    def test_both_classifiers_read_the_coerced_value(self):
        assert 'coerce_int "$FLUX_NOT_READY" 1' in self.SRC
        assert 'coerce_int "$FLUX_NOT_READY_REG" 1' in self.SRC
        assert '"$FLUX_NOT_READY_NUM"' in self.SRC

    def test_the_json_field_can_still_be_null(self):
        assert 'coerce_int "$FLUX_NOT_READY" null' in self.SRC

    def test_no_call_site_hand_rolls_the_digit_case(self):
        # Every coercion goes through coerce_int, so the verdict and the JSON
        # fallbacks cannot drift apart per site.
        assert "*[!0-9]*)" not in self.SRC

    def test_the_alert_and_event_call_sites_coerce(self):
        assert 'coerce_int "$ALERTS_FIRING" null' in self.SRC
        assert 'coerce_int "$(probe_warning_events)" null' in self.SRC

    def test_an_unaskable_alertmanager_degrades_in_both_modes(self):
        # Fallback 1, never 0: a verdict input of 0 would read a snapshot that
        # measured nothing as clean.
        assert 'coerce_int "$ALERTS_FIRING" 1' in self.SRC
        assert 'coerce_int "$ALERTS_FIRING_REG" 1' in self.SRC
        assert 'coerce_int "$ALERTS_FIRING" 0' not in self.SRC
        assert 'coerce_int "$ALERTS_FIRING_REG" 0' not in self.SRC


class TestGitlabOkCallSites:
    """gitlab_ok sits after zfs_degraded in both classifiers; a call that drops
    it shifts every later argument into the wrong slot."""

    SRC = _code(COLLECT_STATE.read_text().splitlines())

    def test_the_json_classifier_reads_gitlab_ok(self):
        assert '"$ZFS_DEGRADED" "$GITLAB_OK_NUM"' in self.SRC

    def test_both_regular_classifiers_read_gitlab_ok(self):
        assert self.SRC.count('"$ZFS_DEGRADED_REG" "$GITLAB_OK_REG"') == 2
