#!/usr/bin/env python3
"""Validate terraform/tailscale/policy.hujson before the supervised apply, where
a wrong policy severs tailnet SSH. Asserts the five top-level keys, a tagOwners
entry per `tag:`, and autoApprovers.routes equal to the advertised routes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent
POLICY = "terraform/tailscale/policy.hujson"
INVENTORY_VAR_DIRS = (
    "ansible/inventories/prod/group_vars",
    "ansible/inventories/prod/host_vars",
)
REQUIRED_KEYS = ("groups", "tagOwners", "acls", "ssh", "autoApprovers")

# `tag:name` anywhere in a policy string. A dst is written `tag:k8s:53,443`, so
# the port suffix has to be excluded rather than assumed absent.
TAG_RE = re.compile(r"tag:[A-Za-z0-9][A-Za-z0-9-]*")
BACKSLASH = chr(92)


def strip_hujson(src: str) -> str:
    """Drop HuJSON's comment and trailing-comma extensions.

    A scanner rather than a regex, so `//` inside a string value is not read as a
    comment. Two passes: a trailing comma is often followed by a comment.
    """
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(src):
        c = src[i]
        if in_str:
            out.append(c)
            if c == BACKSLASH:
                out.append(src[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            if i == -1:
                break
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated block comment")
            i = end + 2
            continue
        out.append(c)
        i += 1

    stripped = "".join(out)
    kept: list[str] = []
    in_str = False
    i = 0
    while i < len(stripped):
        c = stripped[i]
        if in_str:
            kept.append(c)
            if c == BACKSLASH:
                kept.append(stripped[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == ",":
            j = i + 1
            while j < len(stripped) and stripped[j].isspace():
                j += 1
            if j < len(stripped) and stripped[j] in "}]":
                i += 1
                continue
        kept.append(c)
        i += 1
    return "".join(kept)


def _strings(node) -> list[str]:
    """Every string in the document, keys included — a tag may appear as either."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in _strings(k) + _strings(v)]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return []


class OperatorError(RuntimeError):
    """An input the gate cannot read — exit 2, never the exit 1 a caller reads
    as an unsafe policy."""


def advertised_routes(root: Path = REPO) -> set[str]:
    """Every CIDR any inventory group/host advertises as a subnet route."""
    routes: set[str] = set()
    for rel in INVENTORY_VAR_DIRS:
        # rglob: group_vars/<group>/<file>.yml is as valid as group_vars/<group>.yml.
        var_dir = root / rel
        for path in sorted(var_dir.rglob("*.yml")) + sorted(var_dir.rglob("*.yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeDecodeError) as exc:
                raise OperatorError(f"{path} could not be read: {exc}") from exc
            except yaml.YAMLError as exc:
                raise OperatorError(f"{path} does not parse: {exc}") from exc
            if not isinstance(doc, dict):
                continue
            for cidr in doc.get("tailscale_advertise_routes") or []:
                routes.add(str(cidr))
    return routes


def load_policy(root: Path = REPO) -> tuple[dict | None, str | None]:
    """Return (policy document, error). Exactly one of the two is None."""
    try:
        doc = json.loads(strip_hujson((root / POLICY).read_text(encoding="utf-8")))
    except OSError as exc:
        return None, f"{POLICY} cannot be read: {exc}"
    except ValueError as exc:
        return None, f"{POLICY} is not valid HuJSON: {exc}"
    if not isinstance(doc, dict):
        return None, f"{POLICY} must be a JSON object"
    return doc, None


def check(root: Path = REPO, doc: dict | None = None) -> list[str]:
    """Problems with the policy; empty means it is safe to apply."""
    if doc is None:
        doc, error = load_policy(root)
        if error:
            return [error]

    problems: list[str] = []
    missing = [k for k in REQUIRED_KEYS if k not in doc]
    if missing:
        problems.append(f"{POLICY} is missing top-level key(s): {', '.join(missing)}")
        # The two checks below index those keys; without them there is nothing
        # meaningful left to say.
        return problems

    declared = {t for t in doc["tagOwners"] if isinstance(t, str)}
    referenced = {m for s in _strings(doc) for m in TAG_RE.findall(s)}
    undeclared = sorted(referenced - declared)
    if undeclared:
        problems.append(
            f"{POLICY} references tag(s) with no tagOwners entry: "
            f"{', '.join(undeclared)}. A tag nothing owns can be applied by "
            "nobody, so every rule naming it is dead."
        )

    routes = (doc["autoApprovers"] or {}).get("routes") or {}
    # A route key mapped to an empty approver list passes a key-existence check
    # while approving nothing at failover.
    bad = sorted(
        cidr for cidr, approvers in routes.items()
        if not isinstance(approvers, list) or not approvers
        or any(not isinstance(a, str) or not a for a in approvers)
    )
    if bad:
        problems.append(
            f"{POLICY} autoApprovers.routes maps {', '.join(bad)} to something "
            "other than a nonempty list of approver strings — the route is "
            "named but nothing can auto-approve it."
        )
    approved = set(routes)
    advertised = advertised_routes(root)
    if approved != advertised:
        problems.append(
            f"{POLICY} autoApprovers.routes is {sorted(approved)} but the "
            f"inventory's tailscale_advertise_routes is {sorted(advertised)}. "
            "Un-approved advertised routes need a manual admin approval on every "
            "subnet-router failover; approved-but-unadvertised routes are stale."
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", type=Path, default=REPO)
    args = ap.parse_args(argv)

    try:
        doc, error = load_policy(args.repo)
        if error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
        problems = check(args.repo, doc)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("check-tailscale-policy: FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(
        f"check-tailscale-policy: OK — top-level keys "
        f"{', '.join(sorted(doc))}; tags {', '.join(sorted(doc['tagOwners']))}; "
        f"auto-approved routes {', '.join(sorted(doc['autoApprovers']['routes']))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
