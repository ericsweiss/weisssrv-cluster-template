"""Structural tests for scripts/diagnose-network-issues.sh.

The sweep only ever runs against live hosts, so what is checkable offline is
that it parses, bounds every probe, and fails loudly when its inputs are absent.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "diagnose-network-issues.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_it_parses():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_it_sources_the_shared_helper_and_the_roster():
    """Both are hard requirements: timeout_cmd bounds every probe and hosts.env
    carries the roster, so a missing one must abort rather than sweep nothing."""
    assert 'shell-lib.sh" || {' in TEXT
    assert 'hosts.env" || {' in TEXT


# `ssh` in command position: at the start of a line, or opening a substitution
# or a pipe stage. A mention inside an echo or a comment is not an invocation.
_SSH_CALL = re.compile(r"(?m)^[ \t]*ssh[ \t]|\$\([ \t]*ssh[ \t]|\|[ \t]*ssh[ \t]")


def test_every_ssh_goes_through_the_bounded_wrapper():
    """A bare `ssh` would run unbounded and a wedged host would hang the sweep."""
    bare = [
        line
        for line in TEXT.splitlines()
        if _SSH_CALL.search(line) and "timeout_cmd" not in line
    ]
    assert not bare, f"ssh calls outside the bounded wrapper: {bare}"


def test_the_wrapper_bounds_each_call():
    assert "timeout_cmd 10 ssh" in TEXT


def test_it_does_not_set_errexit():
    """An unreachable host must not stop the remaining sections."""
    assert not re.search(r"^set -e", TEXT, re.M)


def test_the_vip_read_fails_loudly():
    """A silent cluster-config read failure would leave the ARP section
    matching nothing and reporting every host clean."""
    assert "cannot read the VIPs from cluster-config.yaml" in TEXT


CODE = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))


def test_it_reads_the_generated_roster_variables():
    for var in ("PVE_IPS", "DNS_IPS", "MAIL_IPS", "K3S_SERVERS"):
        assert re.search(rf"\$(?:{var}\b|\{{{var}\}})", CODE), (
            f"{var} is not expanded, so part of the estate is never swept"
        )


def _vip_filter(dns: str, mail: str, vips: str, neigh: str) -> list[str]:
    """Run the script's VIP_ARP_RE derivation and filter `neigh` through it."""
    start = TEXT.index('VIP_ARP_RE="$(')
    block = TEXT[start:TEXT.index(')"\n', start) + 2]
    script = (
        f"DNS_IPS={shlex.quote(dns)}\nMAIL_IPS={shlex.quote(mail)}\n"
        f"VIP_EXTRA_IPS={shlex.quote(vips)}\n{block}\n"
        'printf "%s\\n" "$NEIGH" | grep -E "$VIP_ARP_RE" || true\n'
    )
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "NEIGH": neigh}, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout.splitlines()


def test_the_vip_filter_matches_whole_addresses_only():
    neigh = "\n".join([
        "192.0.2.100 dev vmbr0 lladdr aa:bb REACHABLE",
        "192.0.20.7 dev vmbr0 lladdr aa:bc STALE",
        "192.0.2.1 dev vmbr0 lladdr aa:bd REACHABLE",
        "192.0.2.10 dev vmbr0 lladdr aa:be REACHABLE",
        "192.0.2.150 dev vmbr0 lladdr aa:bf REACHABLE",
    ])
    assert _vip_filter("192.0.2.150", "", "192.0.2.100 192.0.2.1", neigh) == [
        "192.0.2.100 dev vmbr0 lladdr aa:bb REACHABLE",
        "192.0.2.1 dev vmbr0 lladdr aa:bd REACHABLE",
        "192.0.2.150 dev vmbr0 lladdr aa:bf REACHABLE",
    ]
