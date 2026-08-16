#!/usr/bin/env python3
"""Assert the backup-artifact app list and its alert arms stay paired.

The collector's app list is site data (`nas_storage_backup_artifact_apps`); the
matching `absent(backup_artifact_last_mtime_seconds{app="..."})` arms are
hand-enumerated in the BackupArtifactStale rule. They live in different
lifecycles (Ansible deploy vs Flux reconcile) and both directions fail silently:
an app with no absent() arm emits NO series when its landing dir is never
created, so the freshness arm has nothing to fire on; an arm left behind fires
forever. `companions:` and BackupArtifactCompanionMissing are the same pairing —
a companion rule with no declaring app reads as active DR coverage that can
never fire.

Both file paths are site data and come from flags. Exit 0 when the sets match,
1 otherwise.

  check-backup-artifact-apps.py --host-vars FILE --rules FILE
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

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

    Read as text rather than through the YAML tree: the rule lives inside a
    HelmRelease `values:` blob whose PrometheusRule groups are several levels
    deep and carry Go-template `{{ $labels }}` strings, so a structural walk
    buys nothing over scoping to the alert's own block.
    """
    lines = rules_text.splitlines()
    start = _alert_start(lines, ALERT)
    if start is None:
        raise SystemExit(f"ERROR: no `alert: {ALERT}` rule found in the rules file")
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
            f"nas_storage_backup_artifact_apps declares `companions:`. The "
            f"collector emits backup_artifact_companion_* only per declared "
            f"companion, so the rule has zero series to match and can NEVER "
            f"fire — it reads as active restore-dependency coverage and is "
            f"not.\n"
            f"    Fix: declare the companion(s) in {host_vars} "
            f"(e.g. gitlab-secrets.json on the gitlab entry), or delete the "
            f"rule from {rules}."
        )
    if declared and not have_alert:
        named = ", ".join(f"{a} ({', '.join(g)})" for a, g in sorted(declared.items()))
        problems.append(
            f"apps declare `companions:` but {COMPANION_ALERT} does not exist: "
            f"{named}. The collector emits the series and nothing alerts on "
            f"them, so a missing restore dependency is silent.\n"
            f"    Fix: restore the rule in {rules}, or drop "
            f"the `companions:` keys."
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
    args = parser.parse_args(argv)

    for path in (args.host_vars, args.rules):
        if not path.is_file():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 2

    host_vars_text = args.host_vars.read_text()
    rules_text = args.rules.read_text()
    collector = collector_apps(host_vars_text)
    arms = alert_arm_apps(rules_text)
    companion_problems = check_companions(host_vars_text, rules_text, args.host_vars, args.rules)

    if collector == arms and not companion_problems:
        declared = collector_companions(host_vars_text)
        print(
            f"backup-artifact apps in sync: {len(collector)} app(s) "
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
