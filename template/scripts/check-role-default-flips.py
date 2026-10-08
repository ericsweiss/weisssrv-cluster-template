#!/usr/bin/env python3
"""Report boolean role defaults that flip between two weisssrv.infra refs.
Role variables are default-guarded, so a flip changes behaviour here with no
inventory edit. Run at a pin bump. Exit 0 clean, 1 flips, 2 inspected nothing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent
COLLECTION = "ansible_collections/weisssrv/infra"
ROLES = f"{COLLECTION}/roles"
INVENTORY = REPO / "ansible/inventories/prod"


class Vacuous(Exception):
    """The gate could not inspect its subject — exit 2, never a silent pass."""


def lib_root(explicit: str | None = None) -> Path:
    path = Path(explicit or os.environ.get("WEISSSRV_LIB_PATH") or REPO.parent / "weisssrv-lib")
    if not (path / COLLECTION).is_dir():
        raise Vacuous(f"{path} carries no weisssrv.infra collection")
    return path


def _git(lib: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(lib), *args], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise Vacuous(f"git {' '.join(args)} in {lib}: {done.stderr.strip()}")
    return done.stdout


def role_defaults(lib: Path, ref: str) -> dict[str, dict]:
    """{role: defaults mapping} for every role the ref ships."""
    names = [
        line.rstrip("/")
        for line in _git(lib, "ls-tree", "--name-only", f"{ref}:{ROLES}").splitlines()
        if line.strip()
    ]
    out: dict[str, dict] = {}
    for role in names:
        relpath = f"{ROLES}/{role}/defaults/main.yml"
        try:
            text = _git(lib, "show", f"{ref}:{relpath}")
        except Vacuous:
            continue
        try:
            parsed = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise Vacuous(f"{ref}:{relpath}: {exc}") from exc
        if isinstance(parsed, dict):
            out[role] = parsed
    if not out:
        raise Vacuous(f"{ref} ships no role defaults — nothing would be compared")
    return out


def flipped_bools(old: dict[str, dict], new: dict[str, dict]) -> list[tuple[str, str, bool, bool]]:
    """(role, key, old, new) for every key that is a bool on both sides and differs."""
    flips = []
    for role, after in sorted(new.items()):
        before = old.get(role) or {}
        for key, value in sorted(after.items()):
            was = before.get(key)
            if isinstance(was, bool) and isinstance(value, bool) and was != value:
                flips.append((role, key, was, value))
    return flips


def inventory_keys(inventory: Path) -> set[str]:
    """Every variable this site declares, across group_vars and host_vars."""
    keys: set[str] = set()
    for path in sorted(inventory.rglob("*.yml")):
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise Vacuous(f"{path}: {exc}") from exc
        if isinstance(parsed, dict):
            keys.update(str(k) for k in parsed)
    if not keys:
        raise Vacuous(f"{inventory} declares no variables — the gate has nothing to check")
    return keys


def unadopted(
    flips: list[tuple[str, str, bool, bool]], declared: set[str], allowed: set[str]
) -> list[str]:
    return [
        f"{role}: {key} flips {was} -> {now} and the inventory declares neither "
        "it nor an override"
        for role, key, was, now in flips
        if key not in declared and key not in allowed
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="old", required=True, metavar="REF", help="installed tag")
    ap.add_argument("--to", dest="new", required=True, metavar="REF", help="new tag")
    ap.add_argument("--lib", default=None, help="weisssrv-lib checkout (default $WEISSSRV_LIB_PATH)")
    ap.add_argument("--inventory", type=Path, default=INVENTORY)
    ap.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="KEY",
        help="accept this flip without an inventory entry; repeatable",
    )
    args = ap.parse_args(argv)

    try:
        lib = lib_root(args.lib)
        flips = flipped_bools(role_defaults(lib, args.old), role_defaults(lib, args.new))
        problems = unadopted(flips, inventory_keys(args.inventory), set(args.allow))
    except Vacuous as exc:
        print(f"check-role-default-flips inspected nothing: {exc}", file=sys.stderr)
        return 2

    if problems:
        print(f"Role defaults flip between {args.old} and {args.new}:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "    Fix: declare the variable in the inventory with the value this "
            "cluster wants, or pass --allow KEY once the new default is adopted "
            "deliberately.",
            file=sys.stderr,
        )
        return 1
    print(
        f"No unadopted boolean default flips between {args.old} and {args.new} "
        f"({len(flips)} flip(s) already declared in the inventory)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
