#!/usr/bin/env python3
"""Assert the backup-artifact app list and its alert arms stay paired.

The collector's app list is site data and the matching absent() arms are
hand-enumerated in the alert rules. Contract: docs/SCRIPTS.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

ALERT = "BackupArtifactStale"
COMPANION_ALERT = "BackupArtifactCompanionMissing"
ARM_RE = re.compile(r'absent\(\s*backup_artifact_last_mtime_seconds\{app="([^"]+)"\}\s*\)')


def collector_apps(host_vars_text: str) -> set[str]:
    """The app names the NAS-side mtime collector is rendered with."""
    data = yaml.safe_load(host_vars_text) or {}
    apps = data.get("nas_storage_backup_artifact_apps") or []
    return {a["name"] for a in apps if isinstance(a, dict) and a.get("name")}


def collector_companions(host_vars_text: str) -> dict[str, list[str]]:
    """{app: [companion glob, ...]} for every app that declares one.

    Apps with no `companions:` key are omitted entirely — they are the claim
    "this dump is self-contained", not a companion set of size zero.
    """
    data = yaml.safe_load(host_vars_text) or {}
    apps = data.get("nas_storage_backup_artifact_apps") or []
    out: dict[str, list[str]] = {}
    for app in apps:
        if not isinstance(app, dict) or not app.get("name"):
            continue
        companions = app.get("companions") or []
        if companions:
            out[app["name"]] = list(companions)
    return out


def _alert_start(lines: list[str], alert: str) -> int | None:
    """Index of an exact, active `- alert: <name>` declaration.

    Exact-match so a commented-out rule or a name that merely starts with the
    expected one (`<name>Legacy`) cannot satisfy the gate.
    """
    pattern = re.compile(rf"""^\s*-\s*alert:\s*["']?{re.escape(alert)}["']?\s*(?:#.*)?$""")
    return next((i for i, line in enumerate(lines) if pattern.match(line)), None)


def alert_exists(rules_text: str, alert: str) -> bool:
    """Whether the rules corpus defines the exact active alert."""
    return _alert_start(rules_text.splitlines(), alert) is not None


def alert_arm_apps(rules_text: str) -> set[str]:
    """The app labels named by BackupArtifactStale's absent() arms.

    Read as text, because the rule sits inside a HelmRelease `values:` blob
    carrying Go templates. Raises LookupError when the alert is absent.
    """
    lines = rules_text.splitlines()
    start = _alert_start(lines, ALERT)
    if start is None:
        raise LookupError(f"no `alert: {ALERT}` rule found in the rules file")
    # The expr block ends at the alert's `for:` key — or at the NEXT alert
    # declaration, since `for:` is optional and its absence must not let a
    # later alert's arms satisfy this one.
    body: list[str] = []
    for ln in lines[start + 1 :]:
        if re.match(r"\s*for:\s", ln) or re.match(r"\s*-\s*alert:\s", ln):
            break
        body.append(ln)
    return set(ARM_RE.findall("\n".join(body)))


def check_companions(
    host_vars_text: str, rules_text: str, host_vars: Path, rules: Path
) -> list[str]:
    """Problems in the companions <-> BackupArtifactCompanionMissing pairing."""
    declared = collector_companions(host_vars_text)
    have_alert = alert_exists(rules_text, COMPANION_ALERT)
    problems: list[str] = []
    if have_alert and not declared:
        problems.append(
            f"{COMPANION_ALERT} is defined but no app in "
            f"nas_storage_backup_artifact_apps declares `companions:`, so the "
            f"rule has no series and can never fire.\n"
            f"    Fix: declare the companion(s) in {host_vars}, or delete the "
            f"rule from {rules}."
        )
    if declared and not have_alert:
        named = ", ".join(f"{a} ({', '.join(g)})" for a, g in sorted(declared.items()))
        problems.append(
            f"apps declare `companions:` but {COMPANION_ALERT} does not "
            f"exist, so a missing restore dependency is silent: {named}.\n"
            f"    Fix: restore the rule in {rules}, or drop the `companions:` "
            f"keys."
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The backup-artifact app list and its alert arms must stay paired.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host-vars", type=Path, required=True,
        help="host_vars file declaring nas_storage_backup_artifact_apps",
    )
    parser.add_argument(
        "--rules", type=Path, required=True,
        help=f"manifest defining the {ALERT} rule",
    )
    parser.add_argument(
        "--allow-empty", action="store_true",
        help="a consumer that collects no backup artefacts passes with nothing paired",
    )
    args = parser.parse_args(argv)

    for path in (args.host_vars, args.rules):
        if not path.is_file():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 2

    host_vars_text = args.host_vars.read_text()
    rules_text = args.rules.read_text()
    collector = collector_apps(host_vars_text)
    stale_alert_present = True
    try:
        arms = alert_arm_apps(rules_text)
    except LookupError as exc:
        # A consumer collecting no artefacts has no such alert by definition.
        if not args.allow_empty:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        arms = set()
        stale_alert_present = False
    companion_problems = check_companions(host_vars_text, rules_text, args.host_vars, args.rules)

    if not collector and not arms:
        if not args.allow_empty:
            print(
                f"ERROR: no app in {args.host_vars} declares "
                f"nas_storage_backup_artifact_apps and {ALERT} has no absent() arm — "
                f"the gate paired nothing; pass --allow-empty if this consumer "
                f"collects no backup artefacts",
                file=sys.stderr,
            )
            return 2
        if stale_alert_present:
            print(
                f"ERROR: {ALERT} exists in {args.rules} but has no absent() arm, so "
                f"it guards nothing; --allow-empty covers a consumer with no backup "
                f"artefacts, which ships no such rule at all.\n"
                f"    Fix: delete the rule, or declare the app(s) it must guard in "
                f"{args.host_vars} and add the matching arm(s)",
                file=sys.stderr,
            )
            return 2

    if collector == arms and not companion_problems:
        declared = collector_companions(host_vars_text)
        empty_note = " (--allow-empty)" if not collector else ""
        print(
            f"backup-artifact apps in sync{empty_note}: {len(collector)} app(s) "
            f"({', '.join(sorted(collector))}); "
            f"{sum(len(v) for v in declared.values())} companion(s) declared "
            f"across {len(declared)} app(s)"
        )
        return 0

    if companion_problems:
        print(f"ERROR: {COMPANION_ALERT} and the declared companions disagree.")
        for problem in companion_problems:
            print(f"  - {problem}")
    if collector == arms:
        return 1

    print(f"ERROR: nas_storage_backup_artifact_apps and {ALERT}'s absent() arms disagree.")
    missing_arm = sorted(collector - arms)
    orphan_arm = sorted(arms - collector)
    if missing_arm:
        print(
            "  Collected but NOT alerted on (a landing dir that is never created "
            "emits no series, so nothing fires): " + ", ".join(missing_arm)
        )
        print(
            f"    Fix: add `or absent(backup_artifact_last_mtime_seconds{{app=\"<name>\"}})` "
            f"to {ALERT} in\n    {args.rules}"
        )
    if orphan_arm:
        print(
            "  Alerted on but NOT collected (the arm can never be satisfied, so "
            "it fires forever): " + ", ".join(orphan_arm)
        )
        print(
            f"    Fix: drop that arm, or re-add the app to nas_storage_backup_artifact_apps in\n"
            f"    {args.host_vars}"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
