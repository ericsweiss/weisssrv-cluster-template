#!/usr/bin/env python3
"""Traefik's websecure trustedIPs equal Cloudflare's published ranges.
Compares the HelmRelease list with api.cloudflare.com/client/v4/ips (or
`--ranges-file`, one CIDR per line). Exit 0 in step, 1 drifted, 2 cannot check.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent
RELEASE = REPO / "kubernetes/infrastructure/controllers/traefik/release.yaml"
# The unauthenticated API endpoint; the www.cloudflare.com/ips-* pages refuse
# non-browser user agents.
UPSTREAM = "https://api.cloudflare.com/client/v4/ips"
USER_AGENT = "weisssrv-check-cloudflare-ips/1"


def pinned_ranges(release: Path) -> list[str]:
    """The trustedIPs the release pins on the websecure entrypoint."""
    doc = yaml.safe_load(release.read_text())
    port = doc["spec"]["values"]["ports"]["websecure"]
    return list(port.get("forwardedHeaders", {}).get("trustedIPs") or [])


def published_ranges(ranges_file: Path | None, timeout: float) -> list[str]:
    """Cloudflare's current ranges, from the file when given, else upstream."""
    if ranges_file is not None:
        text = ranges_file.read_text()
        return [line.strip() for line in text.splitlines() if line.strip()]
    request = urllib.request.Request(UPSTREAM, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        body = json.load(resp)
    result = body.get("result") or {}
    return list(result.get("ipv4_cidrs") or []) + list(result.get("ipv6_cidrs") or [])


def compare(pinned: list[str], published: list[str]) -> list[str]:
    """Human-readable differences; empty when the two sets agree."""
    missing = sorted(set(published) - set(pinned))
    stale = sorted(set(pinned) - set(published))
    out = [f"missing from the release: {c}" for c in missing]
    out += [f"no longer published by Cloudflare: {c}" for c in stale]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", type=Path, default=RELEASE)
    parser.add_argument("--ranges-file", type=Path, default=None, help="offline source: one CIDR per line")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        pinned = pinned_ranges(args.release)
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"ERROR: cannot read trustedIPs from {args.release}: {exc}", file=sys.stderr)
        return 2
    if not pinned:
        print(f"ERROR: {args.release} pins no forwardedHeaders.trustedIPs on websecure", file=sys.stderr)
        return 2
    try:
        published = published_ranges(args.ranges_file, args.timeout)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read Cloudflare's ranges: {exc}", file=sys.stderr)
        return 2
    if not published:
        print("ERROR: Cloudflare's range list came back empty", file=sys.stderr)
        return 2
    differences = compare(pinned, published)
    if differences:
        print("Traefik trustedIPs drift from Cloudflare's published ranges:")
        for line in differences:
            print(f"  - {line}")
        print("Replace the websecure forwardedHeaders.trustedIPs list with:")
        for cidr in published:
            print(f"            - {cidr}")
        return 1
    print(f"Traefik trustedIPs match Cloudflare's {len(published)} published ranges.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
