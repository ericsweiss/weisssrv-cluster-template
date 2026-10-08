#!/usr/bin/env python3
"""Check that the CI `flux` CLI, the cluster-versions ConfigMap and the committed
gotk-components.yaml all pin one Flux version.

Exit 0 clean, 1 a disagreement, 2 the gate could not inspect its subject.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CI_FILE = ".gitlab-ci.yml"
VERSIONS_CM = "kubernetes/infrastructure/sources/versions-configmap.yaml"
GOTK_GLOB = "kubernetes/clusters/*/flux-system/gotk-components.yaml"

# The default set `flux bootstrap` installs; --components overrides it for a
# cluster bootstrapped with --components-extra. Any other set is a distribution
# change, not a version bump.
CONTROLLERS = "source-controller,kustomize-controller,helm-controller,notification-controller"

PIN_RE = re.compile(r'^\s*FLUX_VERSION="([0-9][0-9.]*)"\s*$', re.MULTILINE)
CM_RE = re.compile(r'^\s*flux_version:\s*"?([0-9][0-9.]*)"?\s*$', re.MULTILINE)
GOTK_RE = re.compile(r"^# Flux Version: v([0-9][0-9.]*)\s*$", re.MULTILINE)
COMPONENTS_RE = re.compile(r"^# Components: (.+)$", re.MULTILINE)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root (default: the current directory)")
    parser.add_argument(
        "--components",
        default=CONTROLLERS,
        help="expected gotk component set (extend for a cluster bootstrapped with --components-extra)",
    )
    args = parser.parse_args()
    root = Path(args.root)

    ci = _read(root / CI_FILE)
    if ci is None:
        print(f"ERROR: {CI_FILE} is unreadable — the gate inspected nothing", file=sys.stderr)
        return 2
    cm_text = _read(root / VERSIONS_CM)
    if cm_text is None:
        print(f"ERROR: {VERSIONS_CM} is unreadable — the gate inspected nothing", file=sys.stderr)
        return 2

    # Every pin in each file, so a second job or a second key cannot drift
    # behind the first one the gate happens to read.
    pins = sorted({m.group(1) for m in PIN_RE.finditer(ci)})
    if not pins:
        print(f'ERROR: no FLUX_VERSION="..." pin in {CI_FILE}', file=sys.stderr)
        return 2
    cms = sorted({m.group(1) for m in CM_RE.finditer(cm_text)})
    if not cms:
        print(f"ERROR: no flux_version key in {VERSIONS_CM}", file=sys.stderr)
        return 2

    pin_v = pins[0]
    failed = False
    if len(pins) > 1:
        print(f"{CI_FILE} pins several Flux versions: {', '.join(pins)}.")
        print("Every job installs one CLI; keep a single FLUX_VERSION pin.")
        failed = True
    if len(cms) > 1:
        print(f"{VERSIONS_CM} declares several flux_version values: {', '.join(cms)}.")
        failed = True
    if set(pins) != set(cms):
        print(
            f"FLUX_VERSION pin ({', '.join(pins)}) and {VERSIONS_CM} "
            f"({', '.join(cms)}) disagree."
        )
        print("Bump group_vars/all.yml, run `task flux:sync-versions`, and commit.")
        failed = True

    gotk_files = sorted(root.glob(GOTK_GLOB))
    if not gotk_files:
        # Written by `flux bootstrap`, so a pre-bootstrap repository has none.
        print(f"No {GOTK_GLOB} yet — checked the pin against the ConfigMap only.")
        return 1 if failed else 0

    for gotk in gotk_files:
        rel = gotk.relative_to(root)
        text = _read(gotk) or ""
        headers = sorted({m.group(1) for m in GOTK_RE.finditer(text)})
        if not headers:
            print(f"{rel} has no '# Flux Version:' header.")
            failed = True
        for header in (v for v in headers if v not in pins):
            print(f"FLUX_VERSION pin ({pin_v}) and {rel} ({header}) disagree.")
            print(f"Re-run `flux install --export > {rel}` with a matching CLI.")
            failed = True
        components = [m.group(1).strip() for m in COMPONENTS_RE.finditer(text)]
        if not components:
            print(f"{rel} lists <no '# Components:' header>, not {args.components}.")
            print("A cluster bootstrapped with --components-extra passes the matching --components.")
            failed = True
        for listed in (c for c in components if c != args.components):
            print(f"{rel} lists {listed}, not {args.components}.")
            print("A cluster bootstrapped with --components-extra passes the matching --components.")
            failed = True

    if not failed:
        print(f"Flux pinned at {pin_v} in {CI_FILE}, the versions ConfigMap and gotk-components.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
