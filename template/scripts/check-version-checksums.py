#!/usr/bin/env python3
"""Assert every checksum pin still matches the artefact its version pin names.

Downloads the artefact at `checksum_url`, rendered at the pinned version, and compares
its sha256 to `checksum_var`. Needs network egress. Usage and exits: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

# The registry loader lives in check-versions.py so the two gates that read the
# same registry cannot disagree about where it is. The hyphenated filename is
# not importable normally, hence the spec loader.
_CV_SRC = Path(__file__).resolve().parent / "check-versions.py"
if not _CV_SRC.is_file():
    print(
        "ERROR: check-versions.py must sit next to this script — "
        "vendor both (see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2)
_cv_spec = importlib.util.spec_from_file_location("check_versions", _CV_SRC)
check_versions = importlib.util.module_from_spec(_cv_spec)
_cv_spec.loader.exec_module(check_versions)

FETCH_TIMEOUT_SECONDS = 60


class ConfigError(Exception):
    """The registry, the vars file or an entry cannot be used."""


def read_config(path: Path) -> dict:
    """The registry as a mapping, from a `.py` module or a `.json` file."""
    if not path.exists():
        raise ConfigError(f"{path} not found")
    try:
        return check_versions.read_registry(path)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def checksum_entries(config: dict) -> list[dict]:
    """Registry entries that declare both halves of a checksum pin."""
    entries = []
    for service in config.get("services") or []:
        if not isinstance(service, dict):
            raise ConfigError(f"service entry is not a mapping: {service!r}")
        has_var = bool(service.get("checksum_var"))
        has_url = bool(service.get("checksum_url"))
        if has_var != has_url:
            raise ConfigError(
                f"{service.get('name', service)!r} declares only one of "
                "checksum_var/checksum_url — a checksum pin needs both"
            )
        if has_var:
            if not service.get("var_name"):
                raise ConfigError(
                    f"{service.get('name', service)!r} declares a checksum pin "
                    "but no var_name"
                )
            if service.get("version_file"):
                raise ConfigError(
                    f"{service.get('name')!r} pins its version in a version_file; "
                    "checksum pins are only read from the vars file"
                )
            entries.append(service)
    return entries


def _lookup(vars_data: dict, key: str) -> str:
    """One vars-file value, `parent.child` for a nested pin."""
    node: object = vars_data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"vars file has no key {key!r}")
        node = node[part]
    if not isinstance(node, (str, int)) or isinstance(node, bool) or str(node) == "":
        raise ConfigError(f"vars file key {key!r} is not a non-empty scalar")
    return str(node)


def fetch(url: str) -> bytes:
    """The artefact bytes. Raises ConfigError on anything but a clean fetch."""
    if not url.startswith("https://"):
        raise ConfigError(f"refusing a non-https artefact URL: {url}")
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ConfigError(f"failed to download {url}: {exc}") from exc


def violations(
    entries: list[dict],
    vars_data: dict,
    fetcher: Callable[[str], bytes] | None = None,
) -> list[str]:
    """One message per checksum pin that no longer matches its artefact."""
    fetcher = fetcher or fetch
    out = []
    for entry in entries:
        name = entry.get("name") or entry["var_name"]
        version = _lookup(vars_data, entry["var_name"])
        expected = _lookup(vars_data, entry["checksum_var"])
        try:
            url = entry["checksum_url"].format(version=version)
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(
                f"{name}: checksum_url {entry['checksum_url']!r} is not a usable "
                f"format string ({exc}); only {{version}} is substituted"
            ) from exc
        actual = hashlib.sha256(fetcher(url)).hexdigest()
        if expected.split(":", 1)[-1].strip().lower() != actual:
            out.append(
                f"{name}: {entry['checksum_var']} is {expected}, but {url} "
                f"hashes to sha256:{actual} — recompute the pin"
            )
    return out


def _config_path(args: argparse.Namespace) -> Path:
    """The registry path, resolved the way check-versions.py resolves it."""
    try:
        return check_versions.resolve_config_path(
            explicit=str(args.config) if args.config else None,
            repo_root=args.repo_root,
        )
    except SystemExit as exc:
        raise ConfigError(str(exc).removeprefix("ERROR: ")) from exc


def main(argv: list[str] | None = None) -> int:
    repo_default = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Verify registry checksum pins.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=repo_default)
    parser.add_argument("--vars-file", type=Path, default=None)
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="a registry with no checksum pin is a pass",
    )
    args = parser.parse_args(argv)

    try:
        config = read_config(_config_path(args))
        entries = checksum_entries(config)
        if not entries:
            if args.allow_empty:
                print("No checksum pins declared in the registry.")
                return 0
            raise ConfigError(
                "the registry declares no checksum pin (checksum_var + "
                "checksum_url), so the gate verified nothing; pass --allow-empty "
                "if this consumer pins no checksummed artefact"
            )
        vars_file = args.vars_file or args.repo_root / config.get(
            "vars_file", "ansible/inventories/prod/group_vars/all.yml"
        )
        if not vars_file.exists():
            raise ConfigError(f"{vars_file} not found")
        try:
            vars_data = yaml.safe_load(vars_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"{vars_file}: {exc}") from exc
        if not isinstance(vars_data, dict):
            raise ConfigError(f"{vars_file} top-level is not a mapping")
        found = violations(entries, vars_data)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if found:
        print("Stale checksum pins:", file=sys.stderr)
        for message in found:
            print(f"  - {message}", file=sys.stderr)
        return 1
    print(f"All {len(entries)} checksum pins match their pinned artefact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
