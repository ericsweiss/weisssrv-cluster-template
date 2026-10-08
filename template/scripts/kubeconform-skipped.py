#!/usr/bin/env python3
"""Report the distinct kinds kubeconform SKIPPED (no schema in the catalog).

Reads kubeconform `-output json` on stdin; with a BASELINE any pair outside it
exits 1. Usage and rationale: docs/SCRIPTS.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def skipped_kinds(payload: dict) -> list[str]:
    """Distinct 'apiVersion/Kind' strings for resources kubeconform skipped."""
    return sorted(
        {
            f"{r.get('version') or '?'}/{r.get('kind') or '?'}"
            for r in (payload.get("resources") or [])
            if r.get("status") == "statusSkipped"
        }
    )


def read_baseline(path: str) -> set[str]:
    """Expected-skipped pairs, one per line; `#` comments and blanks ignored."""
    lines = Path(path).read_text().splitlines()
    return {s.strip() for s in lines if s.strip() and not s.lstrip().startswith("#")}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    baseline_path = args[0] if args else ""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: kubeconform json output is not parseable: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("ERROR: kubeconform json output is not an object", file=sys.stderr)
        return 1

    if not (payload.get("resources") or []):
        print("ERROR: kubeconform reported zero resources - the render corpus "
              "was empty, so nothing was schema-validated", file=sys.stderr)
        return 2

    skipped = skipped_kinds(payload)
    if skipped:
        print(f"{len(skipped)} kind(s) skipped — no schema in catalog, UNVALIDATED:")
        for s in skipped:
            print(f"  - {s}")
    else:
        print("All rendered kinds were schema-validated (no skips).")

    if not baseline_path:
        if skipped:
            print("If a kind here is new, vendor its CRD schema or accept the gap.")
        return 0

    try:
        expected = read_baseline(baseline_path)
    except OSError as exc:
        print(f"ERROR: cannot read baseline {baseline_path}: {exc}", file=sys.stderr)
        return 1
    unexpected = sorted(set(skipped) - expected)
    if unexpected:
        print(
            f"ERROR: {len(unexpected)} kind(s) skipped that {baseline_path} does not "
            "list — vendor the CRD schema, or add the pair to the baseline:",
            file=sys.stderr,
        )
        for s in unexpected:
            print(f"  - {s}", file=sys.stderr)
        return 1
    print(f"Every skipped kind is listed in {baseline_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
