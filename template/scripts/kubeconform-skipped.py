#!/usr/bin/env python3
"""Print the distinct kinds kubeconform SKIPPED (no schema in the catalog).

flux-lint runs kubeconform with `-ignore-missing-schemas`, which silently marks
any CR whose CRD schema is absent from the datreeio catalog as "skipped" — so a
new CRD-backed kind can start shipping with zero schema validation and no signal
at review time. This reads kubeconform's `-output json` from stdin and lists the
distinct skipped api-version/kind pairs, so a NEW unvalidated kind is visible in
the flux-lint log. Informational only (flux-lint pipes it with `|| true`); the
per-Kustomization kubeconform passes remain the actual gate.

Usage: kubeconform ... -output json | scripts/kubeconform-skipped.py
"""
from __future__ import annotations

import json
import sys


def skipped_kinds(payload: dict) -> list[str]:
    """Distinct 'apiVersion/Kind' strings for resources kubeconform skipped."""
    return sorted(
        {
            f"{r.get('version') or '?'}/{r.get('kind') or '?'}"
            for r in (payload.get("resources") or [])
            if r.get("status") == "statusSkipped"
        }
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("(could not parse kubeconform json output — skipping tracking)")
        return 0
    skipped = skipped_kinds(payload)
    if skipped:
        print(f"{len(skipped)} kind(s) skipped — no schema in catalog, UNVALIDATED:")
        for s in skipped:
            print(f"  - {s}")
        print("If a kind here is new, vendor its CRD schema or accept the gap.")
    else:
        print("All rendered kinds were schema-validated (no skips).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
