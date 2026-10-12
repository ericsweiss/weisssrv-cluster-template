#!/usr/bin/env python3
"""Install pinned CI tool binaries into a workspace bin, verified by sha256.

Stdlib only, no root and no package manager. Usage, pins and exit codes:
docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

FETCH_TIMEOUT_SECONDS = 120
BINARY_MODE = 0o755
RAW = "raw"
ARCHIVE_MODES = {"tar.gz": "r:gz", "tar.xz": "r:xz"}
ASSETS_DIR = ".assets"
# Read-only, a directory, and the final component not followed, so a rename
# through this descriptor stays inside the cache.
OPEN_CACHE_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


class FetchError(Exception):
    """A pin, a download or an archive the script cannot use."""


@dataclass(frozen=True)
class Tool:
    """One pinned download: where it comes from, and what to take out of it."""

    version: str
    url: str
    sha256: str
    kind: str
    member: str = ""


# linux-amd64 pins. Every sha256 is computed from the asset itself and
# cross-checked against the project's published checksums; shellcheck
# publishes none. `{version}` renders from the effective version.

# This table is the one place a tool bump lands: every ci/ template and example
# workflow installing the same tool another way is held equal to it by
# tests/test_pin_parity.py.
TOOLS: dict[str, Tool] = {
    "amtool": Tool(
        version="0.34.1",
        url="https://github.com/prometheus/alertmanager/releases/download/v{version}/alertmanager-{version}.linux-amd64.tar.gz",
        sha256="265b9d1e55ef0d5306a436018af6d2b686c2ce051f03d968f7464ecb1372a7e8",
        kind="tar.gz",
        member="alertmanager-{version}.linux-amd64/amtool",
    ),
    "jq": Tool(
        version="1.8.2",
        url="https://github.com/jqlang/jq/releases/download/jq-{version}/jq-linux-amd64",
        sha256="b1c22172dd303f3be49e935aa56aa48a8b7a46e0bc838b4997d3bb451495870f",
        kind=RAW,
    ),
    "kubeconform": Tool(
        version="0.8.0",
        url="https://github.com/yannh/kubeconform/releases/download/v{version}/kubeconform-linux-amd64.tar.gz",
        sha256="9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883",
        kind="tar.gz",
        member="kubeconform",
    ),
    "kustomize": Tool(
        version="5.8.2",
        url="https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv{version}/kustomize_v{version}_linux_amd64.tar.gz",
        sha256="06af0a202c2b831207d0173f9c9cdb1b30abceca0747cb3fbb72792d26055c95",
        kind="tar.gz",
        member="kustomize",
    ),
    "promtool": Tool(
        version="3.15.0",
        url="https://github.com/prometheus/prometheus/releases/download/v{version}/prometheus-{version}.linux-amd64.tar.gz",
        sha256="2a542df32eac02ee17b9d844fb2aa1de00dafa5476579ba8a3ba862e9d572ea0",
        kind="tar.gz",
        member="prometheus-{version}.linux-amd64/promtool",
    ),
    "shellcheck": Tool(
        version="0.11.0",
        url="https://github.com/koalaman/shellcheck/releases/download/v{version}/shellcheck-v{version}.linux.x86_64.tar.xz",
        sha256="8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198",
        kind="tar.xz",
        member="shellcheck-v{version}/shellcheck",
    ),
    "terraform": Tool(
        version="1.16.5",
        url="https://releases.hashicorp.com/terraform/{version}/terraform_{version}_linux_amd64.zip",
        sha256="2bc2fcfff033265c9e02ca0351f01794eb122f62a9b2a49a3294b9e49eaab5e4",
        kind="zip",
        member="terraform",
    ),
}


def default_dir(env: dict | None = None) -> Path:
    """`$CI_PROJECT_DIR/.bin` when that is set, else `./.bin`."""
    env = os.environ if env is None else env
    root = env.get("CI_PROJECT_DIR")
    return Path(root) / ".bin" if root else Path(".bin")


def resolve(name: str, tool: Tool, env: dict | None = None) -> Tool:
    """The tool with `TOOL_<NAME>_VERSION` / `TOOL_<NAME>_SHA256` applied."""
    env = os.environ if env is None else env
    key = name.upper()
    version = env.get("TOOL_%s_VERSION" % key)
    sha256 = env.get("TOOL_%s_SHA256" % key)
    if version and not sha256:
        raise FetchError(
            "TOOL_%s_VERSION pins %s at %s but TOOL_%s_SHA256 is unset — an "
            "overridden version needs its own checksum" % (key, name, version, key)
        )
    return replace(tool, version=version or tool.version, sha256=sha256 or tool.sha256)


def download(url: str, target: Path) -> None:
    """The asset at `url`, written to `target`."""
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
            with target.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise FetchError("failed to download %s: %s" % (url, exc)) from exc


def digest(path: Path) -> str:
    """The sha256 of `path`, hex, read in chunks so a large asset fits."""
    found = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            found.update(chunk)
    return found.hexdigest()


def verify(path: Path, expected: str, label: str) -> None:
    """Raises unless `path` hashes to `expected`."""
    found = digest(path)
    if found != expected.strip().lower():
        raise FetchError(
            "sha256 mismatch for %s: pinned %s, downloaded %s"
            % (label, expected, found)
        )


def safe_member(member: str) -> str:
    """The member path, refused when absolute or climbing out of the archive."""
    if not member or Path(member).is_absolute() or ".." in Path(member).parts:
        raise FetchError("refusing archive member %r: it escapes the archive" % member)
    return member


def _copy_out(source, target: Path) -> None:
    """One archive member's reader, written to `target`."""
    with target.open("wb") as handle:
        shutil.copyfileobj(source, handle)


def _extract_tar(archive: Path, mode: str, member: str, target: Path) -> None:
    with tarfile.open(archive, mode) as bundle:
        try:
            info = bundle.getmember(member)
        except KeyError:
            raise FetchError("%s has no member %r" % (archive.name, member)) from None
        if not info.isfile():
            raise FetchError("%r is not a regular file" % member)
        with bundle.extractfile(info) as source:
            _copy_out(source, target)


def _extract_zip(archive: Path, member: str, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        try:
            info = bundle.getinfo(member)
        except KeyError:
            raise FetchError("%s has no member %r" % (archive.name, member)) from None
        if info.is_dir():
            raise FetchError("%r is not a regular file" % member)
        with bundle.open(info) as source:
            _copy_out(source, target)


def extract(archive: Path, tool: Tool, target: Path) -> None:
    """Write the archive's single named member to `target`."""
    member = safe_member(tool.member.format(version=tool.version))
    try:
        if tool.kind in ARCHIVE_MODES:
            _extract_tar(archive, ARCHIVE_MODES[tool.kind], member, target)
        elif tool.kind == "zip":
            _extract_zip(archive, member, target)
        else:
            raise FetchError("unknown archive kind %r" % tool.kind)
    except (tarfile.TarError, zipfile.BadZipFile) as exc:
        raise FetchError("cannot read %s: %s" % (archive.name, exc)) from exc


def cache_root(directory: Path) -> Path:
    """The install directory with its own symlinks resolved, as a write finds it."""
    return directory.resolve()


def in_cache(directory: Path, path: Path) -> Path:
    """`path`, refused when a link would carry a write out of `directory`.

    The install directory is a mutable cache, so a link restored into it must
    not redirect an asset or a stamp somewhere else.
    """
    if path.is_symlink():
        raise FetchError(
            "refusing the symlink at %s: the cache holds its own files" % path
        )
    root = cache_root(directory)
    if not path.parent.resolve().is_relative_to(root):
        raise FetchError(
            "refusing %s: it resolves outside the cache at %s" % (path, root)
        )
    return path


def replace_into(directory: Path, source: Path, name: str) -> None:
    """Rename `source` onto `name` in `directory`, through the directory itself.

    The descriptor carries the rename where the platform has one, so a link
    swapped in after the check cannot redirect it.
    """
    # os.py registers renameat as `rename`, and os.replace makes the same call,
    # so that entry is the probe for a rename taking a directory descriptor.
    if os.rename not in os.supports_dir_fd:
        raise FetchError(
            "this platform cannot rename through a directory descriptor, so a "
            "cache write cannot be held inside the install directory; refusing"
        )
    handle = os.open(directory, OPEN_CACHE_FLAGS)
    try:
        os.replace(source, name, dst_dir_fd=handle)
    finally:
        os.close(handle)


def stamp_path(directory: Path, name: str) -> Path:
    """Where the installed version and checksum are recorded."""
    return directory / ("%s.version" % name)


def stamp_text(tool: Tool, installed: str) -> str:
    """The effective version, the pinned asset's digest and the binary's own."""
    return "%s %s %s\n" % (tool.version, tool.sha256.strip().lower(), installed)


def asset_path(directory: Path, name: str) -> Path:
    """Where the verified download is kept for the next run."""
    return directory / ASSETS_DIR / name


def cached_asset(name: str, tool: Tool, directory: Path) -> Path | None:
    """The kept asset, while it still hashes to the pin in this file.

    The directory is mutable, so an asset that no longer matches reads as
    absent and the download happens again.
    """
    asset = in_cache(directory, asset_path(directory, name))
    try:
        return asset if digest(asset) == tool.sha256.strip().lower() else None
    except OSError:
        return None


def obtain_asset(
    name: str, tool: Tool, directory: Path, staging: Path
) -> tuple[Path, bool]:
    """The pinned asset, and whether it had to be downloaded."""
    cached = cached_asset(name, tool, directory)
    if cached is not None:
        return cached, False
    url = tool.url.format(version=tool.version)
    fresh = staging / "asset"
    download(url, fresh)
    verify(fresh, tool.sha256, "%s %s (%s)" % (name, tool.version, url))
    return fresh, True


def clear_path(path: Path) -> None:
    """Make way for a rename: a directory is the one thing it cannot replace."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)


def keep_asset(asset: Path, directory: Path, name: str) -> None:
    """Move a verified asset into the cache, replacing any earlier one."""
    kept = in_cache(directory, asset_path(directory, name))
    kept.parent.mkdir(parents=True, exist_ok=True)
    clear_path(kept)
    replace_into(kept.parent, asset, name)


def unpack(name: str, tool: Tool, asset: Path, staging: Path) -> Path:
    """The executable the asset carries, materialized under `staging`."""
    binary = staging / name
    if tool.kind == RAW:
        shutil.copyfile(asset, binary)
    else:
        extract(asset, tool, binary)
    binary.chmod(BINARY_MODE)
    return binary


def is_runnable(path: Path) -> bool:
    """Whether `path` is a plain file with an execute bit, symlinks excluded."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and bool(mode & 0o111)


def is_installed(name: str, tool: Tool, directory: Path, expected: str) -> bool:
    """Whether the pinned tool is already there, bytes included.

    `expected` is the digest of what the verified asset installs, so a stamp in
    the same mutable directory cannot speak for a binary that differs from it.
    """
    binary = directory / name
    if not is_runnable(binary):
        return False
    try:
        return digest(binary) == expected and stamp_path(
            directory, name
        ).read_text() == stamp_text(tool, expected)
    except OSError:
        return False


def install_tool(name: str, tool: Tool, directory: Path, force: bool = False) -> str:
    """Install one tool into `directory`, and return the line to print."""
    destination = directory / name
    stamp = in_cache(directory, stamp_path(directory, name))
    staging = Path(tempfile.mkdtemp(dir=directory, prefix=".fetch-"))
    try:
        asset, fetched = obtain_asset(name, tool, directory, staging)
        binary = unpack(name, tool, asset, staging)
        installed = digest(binary)
        # The asset is kept only once it has yielded a binary, so a failed
        # install leaves nothing behind in `directory`.
        if fetched:
            keep_asset(asset, directory, name)
        if is_installed(name, tool, directory, installed) and not force:
            return "%s %s: present" % (name, tool.version)
        clear_path(destination)
        # The stamp lands after the binary, so an interrupted install reads as
        # absent and re-fetches rather than as the pinned version.
        replace_into(cache_root(directory), binary, name)
        # The stamp takes the same route, so a link swapped onto its path after
        # the check is replaced rather than followed.
        staged_stamp = staging / stamp.name
        staged_stamp.write_text(stamp_text(tool, installed))
        replace_into(cache_root(directory), staged_stamp, stamp.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return "%s %s -> %s" % (name, tool.version, destination)


def pin_table(tools: dict | None = None, env: dict | None = None) -> str:
    """The pinned table as `--list` prints it, with env overrides applied."""
    tools = TOOLS if tools is None else tools
    lines = ["%-12s %-10s %s" % ("TOOL", "VERSION", "SHA256")]
    for name in sorted(tools):
        tool = resolve(name, tools[name], env)
        lines.append("%-12s %-10s %s" % (name, tool.version, tool.sha256[:16]))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install pinned CI tool binaries, verified by sha256."
    )
    parser.add_argument("tools", nargs="*", metavar="TOOL", help="tool names to install")
    parser.add_argument(
        "--dir",
        help="install directory (default $CI_PROJECT_DIR/.bin, else ./.bin)",
    )
    parser.add_argument(
        "--list", action="store_true", help="print the pinned table and exit"
    )
    parser.add_argument(
        "--force", action="store_true", help="re-install a tool already present"
    )
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)

    unknown = [name for name in args.tools if name not in TOOLS]
    if unknown:
        print(
            "ERROR: unknown tool(s): %s. Known: %s"
            % (", ".join(unknown), ", ".join(sorted(TOOLS))),
            file=sys.stderr,
        )
        return 2

    try:
        if args.list:
            print(pin_table())
            return 0
        if not args.tools:
            print("ERROR: name at least one tool, or pass --list", file=sys.stderr)
            return 2
        directory = Path(args.dir) if args.dir else default_dir()
        directory.mkdir(parents=True, exist_ok=True)
        for name in args.tools:
            print(install_tool(name, resolve(name, TOOLS[name]), directory, args.force))
    except (FetchError, OSError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
