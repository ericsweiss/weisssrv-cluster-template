#!/usr/bin/env python3
"""Compare pinned versions against upstream releases, and write the new pins.

Reads a consumer config and the vars file it names; exits 0 current, 1 outdated
or unwritable, 2 on error. Contract: docs/SCRIPTS.md - check-versions.py.
"""

import argparse
import gzip
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable, Optional

# --- Configuration ---

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_CANDIDATES = ("scripts/version-registry.py", "scripts/version-registry.json")

# All populated by load_config(); see its docstring for the schema.
SERVICE_REGISTRY: list[dict] = []
VARS_FILE: Path = REPO_ROOT / "ansible/inventories/prod/group_vars/all.yml"
# Aliases for `version_file` values that are not repo-relative paths, e.g.
# {"ci": ".gitlab-ci.yml"} for digest-locked `image:` pins in the CI file.
VERSION_FILE_ALIASES: dict[str, Path] = {}
DEFAULT_DEPLOY_COMMAND = ""
UNTRACKED_ALLOWLIST: set[str] = set()
# Heading on the table report; consumer-named so the output is not this
# library's.
REPORT_TITLE = "Version Check Report"

CACHE_DIR = REPO_ROOT / ".version-cache"
CACHE_TTL = 3600  # 1 hour cache

# GitHub API rate limit handling
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GH_API_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")

# Request timeout in seconds
REQUEST_TIMEOUT = 15

# Bounded retry on transient failures only (URLError, socket.timeout, HTTP 5xx)
# — never on 4xx, so a 403 rate-limit is surfaced rather than masked as a blip.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.5  # seconds; multiplied by attempt number for linear backoff


@dataclass
class ServiceVersion:
    """Represents a tracked service and its version information."""
    name: str
    category: str  # github, container, helm, apt, manual
    current_version: str
    latest_version: Optional[str] = None
    update_available: bool = False
    source_url: str = ""
    release_url: str = ""
    error: Optional[str] = None
    var_name: str = ""  # Key in the vars file (or the version_file pin)
    notes: str = ""
    # A held update is reported but not actionable: it does not flip the exit
    # code or trigger MR comments. The registry entry documents why in notes.
    held: bool = False
    # The pin lives where this repo cannot read it, so `current_version` is
    # meaningless and a plain entry would report an update forever. Reported
    # with the upstream release, not actionable; notes says where the pin is.
    unreadable_current: bool = False
    # True only when this check performed a live network fetch (not a cache
    # hit or a manual/no-check service). Lets check_all skip the rate-limit
    # sleep on cache hits.
    fetched_live: bool = False
    # Pins a human must rewrite with this one (a checksum). Written anyway,
    # then flagged PAIRED-EDIT-REQUIRED; --check-partner-pins reds a stale one.
    coupled_vars: list[str] = field(default_factory=list)
    # Pins this script CAN compute: a `<version>-r1` revision pin, and a git SHA
    # resolved from a tag. Moved in the same edit as the version.
    revision_var: str = ""
    sha_var: dict = field(default_factory=dict)


# --- Consumer config ---

def resolve_config_path(explicit: Optional[str] = None, repo_root: Path = REPO_ROOT) -> Path:
    """First of: --config, $CHECK_VERSIONS_CONFIG, the default candidates."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("CHECK_VERSIONS_CONFIG")
    if env:
        return Path(env)
    for candidate in DEFAULT_CONFIG_CANDIDATES:
        path = repo_root / candidate
        if path.exists():
            return path
    raise SystemExit(
        "ERROR: no version-registry config found. Pass --config, set "
        "$CHECK_VERSIONS_CONFIG, or add one of: "
        + ", ".join(DEFAULT_CONFIG_CANDIDATES)
    )


def _read_config(path: Path) -> dict:
    """Parse a .json config, or import a .py module exposing CONFIG or SERVICE_REGISTRY.

    The Python form lets a consumer's registry keep inline comments; it is repo
    data, loaded with the same trust as this script.
    """
    if path.suffix == ".json":
        with path.open() as f:
            return json.load(f)
    if path.suffix == ".py":
        import importlib.util

        spec = importlib.util.spec_from_file_location("check_versions_config", path)
        if not spec or not spec.loader:
            raise SystemExit(f"ERROR: cannot import config {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cfg = getattr(module, "CONFIG", None)
        if cfg is None:
            registry = getattr(module, "SERVICE_REGISTRY", None)
            if registry is None:
                raise SystemExit(
                    f"ERROR: {path} defines neither CONFIG nor SERVICE_REGISTRY"
                )
            cfg = {"services": registry}
        return cfg
    raise SystemExit(f"ERROR: unsupported config format {path.suffix!r} (use .py or .json)")


def read_registry(path: Path) -> dict:
    """The registry as a mapping, for the gates that read it without loading it.

    Raises ValueError on anything that cannot be used, so a caller does not have
    to catch `_read_config`'s SystemExit.
    """
    try:
        config = _read_config(Path(path))
    except SystemExit as exc:
        raise ValueError(str(exc).removeprefix("ERROR: ")) from exc
    except Exception as exc:  # noqa: BLE001 - any load failure is operator error
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"{path}: the registry must define a mapping")
    return config


def load_config(path: Path, repo_root: Optional[Path] = None) -> dict:
    """Populate the module-level config globals from a consumer config file.

    Schema: weisssrv-lib docs/SCRIPTS.md - check-versions.py; worked example
    examples/version-registry.example.py. Paths resolve against `repo_root`.
    """
    global SERVICE_REGISTRY, VARS_FILE, VERSION_FILE_ALIASES, CACHE_DIR
    global DEFAULT_DEPLOY_COMMAND, UNTRACKED_ALLOWLIST, REPO_ROOT, REPORT_TITLE

    cfg = _read_config(Path(path))
    if not isinstance(cfg, dict):
        raise SystemExit(f"ERROR: {path}: config must be a mapping")
    services = cfg.get("services")
    if not isinstance(services, list) or not services:
        raise SystemExit(f"ERROR: {path}: `services` must be a non-empty list")
    for svc in services:
        if not isinstance(svc, dict) or not svc.get("var_name") or not svc.get("name"):
            raise SystemExit(f"ERROR: {path}: service entry needs name + var_name: {svc!r}")

    if repo_root is not None:
        REPO_ROOT = Path(repo_root)
    elif cfg.get("repo_root"):
        REPO_ROOT = Path(cfg["repo_root"])

    SERVICE_REGISTRY = services
    if cfg.get("vars_file"):
        VARS_FILE = REPO_ROOT / cfg["vars_file"]
    VERSION_FILE_ALIASES = {
        alias: REPO_ROOT / rel for alias, rel in (cfg.get("version_file_aliases") or {}).items()
    }
    CACHE_DIR = REPO_ROOT / cfg.get("cache_dir", ".version-cache")
    DEFAULT_DEPLOY_COMMAND = cfg.get("default_deploy_command", "")
    UNTRACKED_ALLOWLIST = set(cfg.get("untracked_allowlist") or [])
    REPORT_TITLE = str(cfg.get("report_title") or REPORT_TITLE)
    return cfg


def missing_registry_entries() -> list[str]:
    """`*_version` pins present in the vars file with no registry entry.

    An untracked pin is never reported as outdated, so `--check-coverage` fails
    on one. Pins with no upstream go in `untracked_allowlist`.
    """
    tracked = {s["var_name"] for s in SERVICE_REGISTRY}
    # A companion pin is tracked BY its service entry, not as an entry of its own.
    for svc in SERVICE_REGISTRY:
        if svc.get("revision_var"):
            tracked.add(svc["revision_var"])
        if (svc.get("sha_var") or {}).get("var"):
            tracked.add(svc["sha_var"]["var"])
    return sorted(
        v for v in read_current_versions()
        if (v.endswith("_version") or v.startswith("helm_chart_versions."))
        and v not in tracked
        and v not in UNTRACKED_ALLOWLIST
    )


def _top_level_pins(text: str) -> dict:
    """Every top-level `key: value` in a vars file, comments and quotes stripped."""
    pins = {}
    for line in text.split("\n"):
        if not line[:1] or line[:1].isspace() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        val = val.strip()
        if "#" in val:
            val = val[: val.index("#")]
        pins[key] = val.strip().strip('"').strip("'")
    return pins


def stale_partner_pins(base_text: str, head_text: str) -> list[str]:
    """Registry pins whose value moved while a coupled partner stayed put."""
    base = _top_level_pins(base_text)
    head = _top_level_pins(head_text)
    stale = []
    for svc in SERVICE_REGISTRY:
        coupled = svc.get("coupled_vars") or []
        var = svc.get("var_name")
        if not coupled or not var or var not in base or var not in head:
            continue
        if base[var] == head[var]:
            continue
        unmoved = [c for c in coupled if c in base and c in head and base[c] == head[c]]
        if unmoved:
            stale.append(
                f"{var}: {base[var]} -> {head[var]}, but {', '.join(unmoved)} "
                f"{'is' if len(unmoved) == 1 else 'are'} unchanged"
            )
    return stale


def resolve_tag_sha(repo: str, tag: str) -> str:
    """The COMMIT a tag resolves to, via `git ls-remote '<repo>' 'refs/tags/<tag>*'`.

    An annotated tag's own object sha is not the commit: the peeled `^{}` line
    is. A bare `refs/tags/<tag>` query hides the peel, so the pattern keeps it.
    """
    proc = subprocess.run(
        ["git", "ls-remote", repo, f"{tag}*"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-remote {repo} {tag} failed: {proc.stderr.strip()}")
    peeled = ""
    plain = ""
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if ref == f"{tag}^{{}}":
            peeled = sha.strip()
        elif ref == tag:
            plain = sha.strip()
    sha = peeled or plain
    if not sha:
        raise RuntimeError(f"{repo}: {tag} resolves to no object")
    return sha


def companion_pins(result: "ServiceVersion") -> list:
    """(var, value) pairs this script computes alongside `result`'s version.

    Raises RuntimeError when a declared sha_var cannot be resolved: an empty
    sha would be written into a build that verifies the tag.
    """
    pairs = []
    version = result.latest_version or ""
    if result.revision_var:
        pairs.append((result.revision_var, f"{version}-r1"))
    sha_var = result.sha_var or {}
    if sha_var.get("var"):
        repo = sha_var.get("repo")
        ref = sha_var.get("ref") or "refs/tags/{version}"
        if not repo:
            raise RuntimeError(f"{result.name}: sha_var has no `repo`")
        pairs.append((sha_var["var"], resolve_tag_sha(repo, ref.format(version=version))))
    return pairs


def write_companion_pins(result: "ServiceVersion") -> list:
    """Write `result`'s computed companions; returns the vars written."""
    written = []
    for var, value in companion_pins(result):
        if not update_version_in_file(var, value):
            raise RuntimeError(f"could not find {var} in {VARS_FILE.name}")
        print(f"  also updated {var} -> {value}")
        written.append(var)
    return written


def _run_check_partner_pins(base_ref: str) -> None:
    """--check-partner-pins: fail while a coupled pin's partner is stale."""
    try:
        rel = VARS_FILE.relative_to(REPO_ROOT)
    except ValueError:
        rel = VARS_FILE
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{base_ref}:{rel.as_posix()}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(
            f"ERROR: cannot read {rel} at {base_ref}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(2)

    stale = stale_partner_pins(proc.stdout, VARS_FILE.read_text())
    if stale:
        print(
            f"ERROR: {len(stale)} version pin(s) moved without their paired value:",
            file=sys.stderr,
        )
        for line in stale:
            print(f"  - {line}", file=sys.stderr)
        print(
            "  Recompute the partner (checksum, digest or git SHA) and commit it "
            "with the version bump.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"No stale paired pins against {base_ref}.")
    sys.exit(0)


# --- HTTP helpers ---

def _urlopen_with_retry_full(req, timeout: int = REQUEST_TIMEOUT) -> tuple[str, bytes]:
    """urlopen with a bounded retry; returns (content_type, body).

    Retries URLError, socket.timeout and HTTP 5xx; 4xx re-raises at once. The
    content type lets a caller tell a real payload from an HTML error page.
    """
    last_exc: Exception
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                return content_type, resp.read()
        except urllib.error.HTTPError as e:
            # 4xx (incl. 403 rate-limit) is not transient — don't retry.
            if e.code < 500:
                raise
            last_exc = e
        except (urllib.error.URLError, socket.timeout) as e:
            last_exc = e
        except http.client.IncompleteRead as e:
            # Mid-body truncation is transient: the next attempt re-reads the body.
            last_exc = e
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_BACKOFF * attempt)
    raise last_exc


def _urlopen_with_retry(req, timeout: int = REQUEST_TIMEOUT) -> bytes:
    """urlopen with a bounded retry on transient failures; return the body.

    Thin wrapper over _urlopen_with_retry_full for callers that only need the
    response body. See that function for retry semantics.
    """
    _content_type, body = _urlopen_with_retry_full(req, timeout=timeout)
    return body


def _make_request(url: str, headers: Optional[dict] = None) -> dict | list | str:
    """Make an HTTP GET request and return parsed JSON or raw text."""
    req_headers = {"User-Agent": "weisssrv-lib-version-check/1.0"}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, headers=req_headers)
    try:
        data = _urlopen_with_retry(req, timeout=REQUEST_TIMEOUT).decode("utf-8")
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data
    except urllib.error.HTTPError as e:
        if e.code == 403:
            remaining = e.headers.get("X-RateLimit-Remaining", "?")
            reset = e.headers.get("X-RateLimit-Reset", "?")
            raise RuntimeError(
                f"HTTP 403 (rate limited?) remaining={remaining} reset={reset}"
            ) from e
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Connection error: {e.reason}") from e
    except Exception as e:
        # check_service catches RuntimeError before its typed fallback, so the
        # type tag must be in the message here or it's lost from the diagnostic.
        raise RuntimeError(f"Request failed ({type(e).__name__}): {e}") from e


def github_api(path: str) -> dict | list:
    """Make a GitHub API request with optional authentication."""
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return _make_request(f"{GITHUB_API}{path}", headers)


def fetch_apt_packages(base_url: str) -> str:
    """Fetch the apt Packages file, falling back to Packages.gz.

    Raises RuntimeError when neither can be fetched.
    """
    req_headers = {"User-Agent": "weisssrv-lib-version-check/1.0"}

    def _is_valid_packages_response(content_type: str, content: str) -> bool:
        """Check if response is a valid Packages file (not an HTML error page)."""
        # A Packages file is text/plain or untyped; an error page is text/html.
        if "text/html" in content_type.lower():
            return False
        if content.strip().startswith("<!DOCTYPE") or content.strip().startswith("<html"):
            return False
        if "Package:" not in content:
            return False
        return True

    # Uncompressed first, through the bounded retry helper; it returns the
    # Content-Type for the HTML-vs-payload sniff below.
    try:
        req = urllib.request.Request(base_url, headers=req_headers)
        content_type, raw = _urlopen_with_retry_full(req, timeout=REQUEST_TIMEOUT)
        content = raw.decode("utf-8")
        if content.strip() and _is_valid_packages_response(content_type, content):
            return content
    except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, UnicodeDecodeError):
        pass

    gz_url = f"{base_url}.gz"
    req = urllib.request.Request(gz_url, headers=req_headers)

    try:
        content_type, compressed_data = _urlopen_with_retry_full(req, timeout=REQUEST_TIMEOUT)
        if "text/html" in content_type.lower():
            raise RuntimeError(f"Received HTML error page instead of Packages.gz from {gz_url}")

        with gzip.GzipFile(fileobj=BytesIO(compressed_data)) as gz:
            content = gz.read().decode("utf-8")
            if not content.strip() or "Package:" not in content:
                raise RuntimeError(f"Invalid or empty Packages file from {gz_url}")
            return content
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Failed to fetch {base_url} or {gz_url}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Connection error fetching apt Packages: {e.reason}") from e
    except socket.timeout as e:
        raise RuntimeError(f"Timeout fetching apt Packages from {gz_url}") from e
    except gzip.BadGzipFile as e:
        raise RuntimeError(f"Invalid gzip data from {gz_url}") from e


# --- Cache helpers ---

def _cache_key(service_name: str) -> Path:
    """Generate a cache file path for a service."""
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", service_name)
    return CACHE_DIR / f"{safe_name}.json"


def _read_cache(service_name: str) -> Optional[str]:
    """Read cached version if still valid."""
    cache_file = _cache_key(service_name)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        if time.time() - data.get("timestamp", 0) < CACHE_TTL:
            return data.get("version")
    except (json.JSONDecodeError, KeyError, OSError) as e:
        # Delete corrupted cache so _write_cache can overwrite cleanly and
        # we don't keep hitting the same broken entry on every run.
        print(
            f"Warning: corrupted cache {cache_file.name}, removing: {e}",
            file=sys.stderr,
        )
        try:
            cache_file.unlink()
        except OSError as e2:
            print(f"Warning: could not remove corrupted cache {cache_file.name}: {e2}", file=sys.stderr)
    return None


def _write_cache(service_name: str, version: str) -> None:
    """Write version to cache."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = _cache_key(service_name)
        cache_file.write_text(json.dumps({
            "version": version,
            "timestamp": time.time(),
            "service": service_name,
        }))
    except OSError as e:
        print(f"Warning: failed to write cache for {service_name}: {e}", file=sys.stderr)


# --- Version parsing ---

def parse_version_tuple(version_str: str) -> tuple:
    """Parse a version string into a comparable tuple of (type_rank, value).

    Handles v-prefixes, Debian epochs and numeric suffixes (k3s10 > k3s9).
    type_rank is 0 for ints and 1 for strings, so ints sort before strings.
    """
    v = version_str.lstrip("v")
    # Drop a Debian epoch prefix (e.g. "1:1.80.0" -> "1.80.0") so the epoch
    # integer is not parsed as a leading version segment.
    epoch_match = re.match(r"^\d+:(.*)$", v)
    if epoch_match:
        v = epoch_match.group(1)
    v = v.replace("+", ".")
    parts = re.split(r"[.\-]", v)
    result = []
    for part in parts:
        # Alternating text/numeric segments so k3s10 > k3s9.
        segments = re.findall(r"(\d+|\D+)", part)
        for seg in segments:
            if seg.isdigit():
                result.append((0, int(seg)))
            else:
                result.append((1, seg))
    return tuple(result)


def version_tuple_greater(a: tuple, b: tuple) -> bool:
    """True when version tuple a is newer than b.

    At the same position a numeric segment beats a string one, so
    "17.1-trixie" > "17-trixie", "17.1" > "17" and "17-alpha" < "17".
    """
    min_len = min(len(a), len(b))
    for i in range(min_len):
        a_type, a_val = a[i]
        b_type, b_val = b[i]

        if a_type == b_type:
            if a_val > b_val:
                return True
            if a_val < b_val:
                return False
        else:
            return a_type < b_type

    if len(a) == len(b):
        return False

    if len(a) > len(b):
        return a[min_len][0] == 0
    else:
        return b[min_len][0] != 0


def version_greater(a: str, b: str) -> bool:
    """Compare parsed version tuples, e.g. "17.1-trixie" > "17-trixie"."""
    try:
        return version_tuple_greater(parse_version_tuple(a), parse_version_tuple(b))
    except (TypeError, ValueError):
        # Fallback to lexicographic — log so operators know comparison quality may be degraded
        print(f"Warning: falling back to string comparison for {a!r} vs {b!r}", file=sys.stderr)
        return a > b


def highest_version(candidates: Iterable[str], *, skip_unparseable: bool = True) -> Optional[str]:
    """The highest of `candidates` by parsed version tuple; None when none qualify.

    An unparseable candidate is skipped by default; pass skip_unparseable=False
    to let its TypeError/ValueError propagate. Ties keep the first seen.
    """
    best: Optional[str] = None
    best_tuple: Optional[tuple] = None
    for candidate in candidates:
        try:
            vtuple = parse_version_tuple(candidate)
        except (TypeError, ValueError):
            if skip_unparseable:
                continue
            raise
        if best_tuple is None or version_tuple_greater(vtuple, best_tuple):
            best_tuple = vtuple
            best = candidate
    return best


# --- Version fetchers ---

def _debian_version_part_compare(a: str, b: str) -> int:
    """Compare one Debian upstream_version or debian_revision part.

    Alternating non-digit and digit chunks per debian-policy 5.6.12: digits
    compare numerically; non-digits lexically, `~` then letters then the rest.
    """
    def order(c: str) -> int:
        # Sort key in a non-digit chunk: '~' before end-of-chunk, which is
        # before letters, which are before every other character.
        if c == "~":
            return -1
        if c == "":
            return 0
        if c.isalpha():
            return ord(c)
        return ord(c) + 256

    i = j = 0
    while i < len(a) or j < len(b):
        sa = ""
        while i < len(a) and not a[i].isdigit():
            sa += a[i]
            i += 1
        sb = ""
        while j < len(b) and not b[j].isdigit():
            sb += b[j]
            j += 1
        for k in range(max(len(sa), len(sb))):
            ca = sa[k] if k < len(sa) else ""
            cb = sb[k] if k < len(sb) else ""
            if order(ca) != order(cb):
                return -1 if order(ca) < order(cb) else 1

        na = ""
        while i < len(a) and a[i].isdigit():
            na += a[i]
            i += 1
        nb = ""
        while j < len(b) and b[j].isdigit():
            nb += b[j]
            j += 1
        if (int(na) if na else 0) != (int(nb) if nb else 0):
            return -1 if (int(na) if na else 0) < (int(nb) if nb else 0) else 1
    return 0


def debian_version_compare(a: str, b: str) -> int:
    """Compare two Debian version strings per debian-policy 5.6.12.

    Returns -1, 0 or +1. Pure Python, so dpkg need not be installed; the
    ordering cases are asserted in tests/test_check_versions.py.
    """
    # Split the epoch (debian-policy §5.6.12: an unsigned integer). A
    # non-integer epoch is malformed and raises — falling back to epoch=0
    # would report a real change as "version unchanged".
    def split(v: str) -> tuple[int, str, str]:
        if ":" in v:
            ep_s, rest = v.split(":", 1)
            try:
                ep = int(ep_s)
            except ValueError as e:
                raise ValueError(
                    f"malformed Debian version {v!r}: epoch prefix "
                    f"{ep_s!r} before ':' must be an unsigned integer"
                ) from e
            if ep < 0:
                raise ValueError(
                    f"malformed Debian version {v!r}: epoch must be "
                    f"non-negative (got {ep})"
                )
        else:
            ep, rest = 0, v
        # Split upstream / debian_revision on LAST '-'
        if "-" in rest:
            up, rev = rest.rsplit("-", 1)
        else:
            up, rev = rest, ""
        return ep, up, rev

    ea, ua, ra = split(a)
    eb, ub, rb = split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    rc = _debian_version_part_compare(ua, ub)
    if rc != 0:
        return rc
    return _debian_version_part_compare(ra, rb)


def _collect_apt_versions(text: str, package: str) -> list[str]:
    """All `Version:` values for `package` in a Debian Packages index.

    Stanzas are blank-line separated. Callers pick their own comparator and
    any pre-release filtering.
    """
    versions: list[str] = []
    in_pkg = False
    for line in text.split("\n"):
        if line.startswith("Package:"):
            in_pkg = line.split(":", 1)[1].strip() == package
        elif in_pkg and line.startswith("Version:"):
            versions.append(line.split(":", 1)[1].strip())
            in_pkg = False
    return versions


def _fetch_packages_index(url: str) -> str:
    """Text of one Debian Packages index, gunzipped when the payload is gzip.

    The payload is sniffed for gzip magic, not the URL suffix, because mirrors
    redirect. A response that is not a Packages file falls back to `<url>.gz`.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "weisssrv-lib-version-check/1.0"})
    raw = _urlopen_with_retry(req, timeout=30)
    text = (
        gzip.decompress(raw).decode("utf-8", errors="replace")
        if raw[:2] == b"\x1f\x8b"
        else raw.decode("utf-8", errors="replace")
    )
    if "Package:" in text:
        return text
    if url.endswith(".gz"):
        raise RuntimeError(f"no Packages stanzas in {url}")
    return fetch_apt_packages(url)


def fetch_apt_repo_version(svc: dict) -> str:
    """Highest version of `apt_package` in a Debian apt repo's Packages index.

    Tracks the apt publish cadence, so a bump is always installable. Entry keys
    `apt_url`, `apt_package` and `apt_exclude_regex`: docs/SCRIPTS.md.
    """
    urls = svc.get("apt_url") or svc["apt_index_url"]
    if isinstance(urls, str):
        urls = [urls]
    pkg = svc["apt_package"]
    exclude = svc.get("apt_exclude_regex", "")

    versions: list[str] = []
    errors: list[str] = []
    for url in urls:
        try:
            text = _fetch_packages_index(url)
        except (RuntimeError, urllib.error.URLError, OSError, gzip.BadGzipFile) as e:
            errors.append(f"{url}: {e}")
            continue
        versions = _collect_apt_versions(text, pkg)
        if exclude:
            versions = [v for v in versions if not re.search(exclude, v, re.IGNORECASE)]
        if versions:
            break

    if not versions:
        detail = f"; attempts: {'; '.join(errors)}" if errors else ""
        raise RuntimeError(f"package '{pkg}' not found in {', '.join(urls)}{detail}")

    # debian-policy ordering: epochs, revisions and `~` pre-release semantics,
    # all of which a plain string-tuple compare gets wrong.
    latest = versions[0]
    for v in versions[1:]:
        if debian_version_compare(v, latest) > 0:
            latest = v
    return latest


def fetch_github_release(svc: dict) -> str:
    """Latest GitHub release version for `github_repo`.

    With tag_filter the highest matching version wins, not the most recently
    published, so a patch on an older branch cannot win. Paginates 5 pages.
    """
    repo = svc["github_repo"]
    tag_filter = svc.get("tag_filter")
    prefix = svc.get("version_prefix", "")
    strip_prefix = svc.get("strip_prefix", False)

    if tag_filter:
        matching_versions = []
        max_pages = 5  # Limit pagination to avoid excessive API calls

        for page in range(1, max_pages + 1):
            releases = github_api(f"/repos/{repo}/releases?per_page=100&page={page}")

            if not releases:
                break

            for release in releases:
                if release.get("draft") or release.get("prerelease"):
                    continue
                tag = release.get("tag_name", "")
                if re.match(tag_filter, tag):
                    version = tag
                    if strip_prefix and prefix and version.startswith(prefix):
                        version = version[len(prefix):]
                    matching_versions.append(version)

            if len(releases) < 100:
                break

        # Highest by version, not most recent by date.
        best = highest_version(matching_versions)
        if best is None:
            raise RuntimeError(f"No release matching {tag_filter}")
        return best
    else:
        # Use latest release endpoint
        release = github_api(f"/repos/{repo}/releases/latest")
        version = release.get("tag_name", "")
        # Fail loud on a missing tag_name, matching the tag_filter branch above.
        # Returning "" here would have the service silently report up-to-date
        # with a blank Latest column (version_greater("", current) is False).
        if not version:
            raise RuntimeError(f"latest release for {repo} has no tag_name")
        if strip_prefix and prefix and version.startswith(prefix):
            version = version[len(prefix):]
        return version


def _dockerhub_best_tag(
    image: str,
    regex: str,
    *,
    version_prefix: str = "",
    pin_major: bool = False,
    current: str = "",
    return_full_tag: bool = False,
    name_filter: str = "",
    max_pages: int = 1,
    page_size: int = 50,
) -> Optional[str]:
    """Highest Docker Hub tag of `image` matching `regex` (group 1 = version).

    return_full_tag yields the tag itself, else the captured group.
    version_prefix and pin_major narrow results; name_filter only the query.
    """
    url = f"https://hub.docker.com/v2/repositories/{image}/tags?page_size={page_size}&ordering=last_updated"
    if name_filter:
        url += f"&name={name_filter}"
    elif version_prefix:
        url += f"&name={version_prefix}"
    # Bounded pagination: a high-churn repo can bury a stable tag past page one
    # even with a name filter, so such callers pass max_pages > 1. The default
    # keeps every other caller at one request.
    results = []
    pages = 0
    while url and pages < max_pages:
        data = _make_request(url)
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected non-JSON response from {url}")
        results.extend(data.get("results", []))
        url = data.get("next")
        pages += 1

    # Major version of `current` for the major pin ("17.2-trixie" -> "17"); a
    # leading "v" is tolerated so v-prefixed schemes stay pinned.
    major_filter = None
    if pin_major and current:
        m = re.match(r"^v?(\d+)", current)
        if m:
            major_filter = m.group(1)

    # Captured version -> the tag it came from; ties keep the first seen.
    candidates: dict[str, str] = {}
    for result in results:
        tag_name = result.get("name", "")
        match = re.match(regex, tag_name)
        if not match:
            continue
        # version_prefix: only consider tags starting with this prefix
        # (e.g. "v1.15." restricts to patch updates within 1.15.x).
        if version_prefix and not tag_name.startswith(version_prefix):
            continue
        # Filter on the CAPTURED version, not the raw tag: a leading "v" must
        # not bypass the major pin. A tag_regex with no capture group compares
        # the whole tag.
        extracted = match.group(1) if match.lastindex else match.group(0)
        if major_filter:
            tag_major = re.match(r"^v?(\d+)", extracted)
            if not tag_major or tag_major.group(1) != major_filter:
                continue  # Skip tags from a different major version
        candidates.setdefault(extracted, tag_name)

    best = highest_version(candidates)
    if best is None:
        return None
    return candidates[best] if return_full_tag else best


def fetch_dockerhub_version(svc: dict) -> str:
    """Latest Docker Hub version for `docker_image`, matched by `tag_regex`.

    Returns the full tag name, which is what the pins store. tag_regex may
    capture the version portion; with no group the whole tag is used.
    """
    image = svc["docker_image"]
    tag_regex = svc.get("tag_regex", r"^(v?\d+(?:\.\d+)*)$")
    best_tag = _dockerhub_best_tag(
        image,
        tag_regex,
        version_prefix=svc.get("version_prefix", ""),
        pin_major=svc.get("pin_major_version", False),
        current=svc.get("_current_version", ""),
        return_full_tag=True,
        name_filter=svc.get("dockerhub_name_filter", ""),
        page_size=svc.get("dockerhub_page_size", 50),
    )
    if best_tag is None:
        raise RuntimeError(f"No matching tags found for {image} (regex: {tag_regex})")
    return best_tag


def fetch_lsio_version(svc: dict) -> str:
    """Latest LinuxServer.io image version, captured by `lsio_version_regex`.

    Their tags carry a `version-` prefix, so the regex captures the version
    portion and that is what is returned.
    """
    image = svc["docker_image"]
    version_regex = svc["lsio_version_regex"]
    best_version = _dockerhub_best_tag(
        image,
        version_regex,
        version_prefix=svc.get("version_prefix", ""),
        name_filter=svc.get("lsio_name_filter", ""),
        max_pages=svc.get("lsio_max_pages", 1),
    )
    if best_version is None:
        raise RuntimeError(
            f"No matching tags found for {image} "
            f"(regex: {version_regex})"
        )
    return best_version


def fetch_ghcr_version(svc: dict) -> str:
    """Latest GHCR tag for `ghcr_image` matching `tag_filter`.

    Uses the anonymous pull-token flow and the Docker Registry tags/list API,
    so public images resolve without a GITHUB_TOKEN.
    """
    image = svc["ghcr_image"]
    tag_filter = svc.get("tag_filter", r"^v?\d+\.\d+")

    token_resp = _make_request(
        f"https://ghcr.io/token?scope=repository:{image}:pull&service=ghcr.io"
    )
    if not isinstance(token_resp, dict) or not token_resp.get("token"):
        raise RuntimeError(f"Could not obtain anonymous pull token for ghcr.io/{image}")

    tags_resp = _make_request(
        f"https://ghcr.io/v2/{image}/tags/list",
        headers={"Authorization": f"Bearer {token_resp['token']}"},
    )
    if not isinstance(tags_resp, dict):
        raise RuntimeError(f"Unexpected non-JSON tag list for ghcr.io/{image}")

    best_version = highest_version(
        tag for tag in (tags_resp.get("tags") or []) if re.match(tag_filter, tag)
    )
    if best_version is None:
        raise RuntimeError(f"No matching tags found for ghcr.io/{image}")

    return best_version


def fetch_helm_version(svc: dict) -> str:
    """Latest chart version for `helm_chart` from `helm_repo`'s index.yaml.

    The index is parsed by hand so PyYAML stays optional.
    """
    repo_url = svc["helm_repo"]
    chart_name = svc["helm_chart"]

    index_url = f"{repo_url}/index.yaml"
    raw = _make_request(index_url)

    if not isinstance(raw, str):
        raise RuntimeError(f"Unexpected response type from {index_url}")

    lines = raw.split("\n")
    in_entries = False
    in_chart = False
    chart_indent = 0
    # Indent of each chart entry's first key. The chart's own `version:` sits
    # at this column; a dependency or maintainer `version:` nests deeper.
    entry_key_indent = None
    versions = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped == "entries:":
            in_entries = True
            continue

        if not in_entries:
            continue

        if not in_chart:
            if line.rstrip().rstrip(":") and stripped.rstrip(":") == chart_name:
                in_chart = True
                chart_indent = len(line) - len(line.lstrip())
                continue
        else:
            if not line or line.isspace():
                continue
            line_indent = len(line) - len(line.lstrip())

            # Same indent as the chart name = the next chart entry.
            if line_indent <= chart_indent and not stripped.startswith("-"):
                break

            # Resolve the key name and the column it starts at. A list-item
            # line ("- key: ...") starts its first key two columns past the
            # dash; that column is the chart entry's key indent.
            if stripped.startswith("- "):
                key_indent = line_indent + 2
                key = stripped[2:].split(":", 1)[0].strip()
            else:
                key_indent = line_indent
                key = stripped.split(":", 1)[0].strip()
            if entry_key_indent is None and stripped.startswith("- "):
                entry_key_indent = key_indent

            # The chart's own "version:" is a direct child of the entry, so
            # match the exact key at entry_key_indent — excluding "appVersion:"
            # and any deeper "version:" under dependencies:/maintainers:.
            if key == "version" and (entry_key_indent is None or key_indent == entry_key_indent):
                ver = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                if not re.search(r"(alpha|beta|rc|dev|snapshot)", ver, re.IGNORECASE):
                    versions.append(ver)

    best = highest_version(versions)
    if best is None:
        raise RuntimeError(f"No versions found for chart {chart_name}")
    return best


# --- Vars-file parser (simple YAML extraction without PyYAML) ---

# var_name -> the version_file paths that resolved no current version. A
# renamed manifest would otherwise drop the pin out of tracking silently.
UNRESOLVED_PINS: dict[str, list[str]] = {}


def read_pinned_image_versions() -> dict[str, str]:
    """Current versions of pins that live outside the vars file.

    A `version_file` entry names an alias or paths; the default matcher is an
    `image:` line, and `pin_regex` replaces it for any other pin shape.
    """
    versions: dict[str, str] = {}
    UNRESOLVED_PINS.clear()
    repo_root = REPO_ROOT
    for svc in SERVICE_REGISTRY:
        version_file = svc.get("version_file")
        if not version_file:
            continue
        if isinstance(version_file, str) and version_file in VERSION_FILE_ALIASES:
            paths = [VERSION_FILE_ALIASES[version_file]]
        elif isinstance(version_file, str):
            paths = [repo_root / version_file]
        else:
            paths = [repo_root / p for p in version_file]
        pin_regex = svc.get("pin_regex")
        if pin_regex:
            if re.compile(pin_regex).groups < 1:
                raise RuntimeError(
                    f"{svc['var_name']}: pin_regex needs one capture group for the version"
                )
        image = svc.get("image_ref") or svc.get("docker_image", "")
        # Collect the tag from every readable path (not break-on-first) so
        # divergent pins between manifests that must share one tag are caught.
        matched: list[tuple[Path, str]] = []
        for path in paths:
            try:
                content = path.read_text()
            except OSError:
                continue
            if pin_regex:
                m = re.search(pin_regex, content, re.MULTILINE)
            else:
                m = re.search(
                    rf"^\s*image:\s*{re.escape(image)}:([\w.+-]+?)(?:@sha256:[0-9a-f]+)?\s*$",
                    content,
                    re.MULTILINE,
                )
            if m:
                matched.append((path, m.group(1)))
        if not matched:
            if not svc.get("unreadable_current"):
                UNRESOLVED_PINS[svc["var_name"]] = [str(p) for p in paths]
            continue
        distinct = {tag for _, tag in matched}
        if len(distinct) > 1:
            detail = ", ".join(
                f"{p.relative_to(repo_root)}={tag}" for p, tag in matched
            )
            # Files that must share one pinned version have diverged - an
            # error, not a pick-first.
            raise RuntimeError(
                f"{svc['var_name']} pins diverge across files "
                f"that must share one version: {detail}"
            )
        versions[svc["var_name"]] = matched[0][1]
    return versions


def read_current_versions() -> dict[str, str]:
    """Pinned versions read out of the vars file, without a YAML parser."""
    content = VARS_FILE.read_text()
    versions = {}

    # Registered pins whose var_name breaks the `*_version` convention (an
    # lxc_template filename, say) are read by exact top-level key match.
    extra_keys = {
        s["var_name"] for s in SERVICE_REGISTRY
        if s.get("var_name") and "_version" not in s["var_name"] and not s.get("version_file")
    }

    in_helm = False

    for line in content.split("\n"):
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        if stripped == "helm_chart_versions:":
            in_helm = True
            continue

        if in_helm:
            if line.startswith("  ") and ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if "#" in val:
                    val = val[:val.index("#")].strip().strip('"').strip("'")
                versions[f"helm_chart_versions.{key}"] = val
            elif not line.startswith(" "):
                in_helm = False

        # Only column-0 keys are pins; an indented `*_version:` belongs to some
        # other mapping and must not be read (or later rewritten) as top level.
        at_top_level = not line[:1].isspace()
        _key = stripped.split(":")[0].strip()
        if not in_helm and at_top_level and ":" in stripped and (
            _key.endswith("_version") or _key in extra_keys
        ):
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # Remove inline comments
            if "#" in val:
                val = val[:val.index("#")].strip().strip('"').strip("'")
            versions[key] = val

    # Digest-locked image pins (version_file entries) live outside the vars file.
    versions.update(read_pinned_image_versions())
    return versions


def _rewrite_pin_line(line: str, key: str, new_version: str, *, always_quote: bool) -> str:
    """One pin line rewritten to `new_version`, keeping its indent and comment.

    A trailing "Currently deployed <v>" note is refreshed with it. The value is
    quoted when always_quote is set or when the old value already was.
    """
    value = line.split(":", 1)[1]
    comment = ""
    if "#" in line:
        comment_text = re.sub(
            r"Currently deployed \S+",
            f"Currently deployed {new_version}",
            line.split("#", 1)[1],
        )
        comment = f"# {comment_text.strip()}" if comment_text.strip() else ""
        if "#" in value:
            value = value[:value.index("#")]
    quoted = always_quote or value.strip().startswith(('"', "'"))
    new_val = f'"{new_version}"' if quoted else new_version
    indent = " " * (len(line) - len(line.lstrip()))
    rewritten = f"{indent}{key}: {new_val}"
    return f"{rewritten}  {comment}" if comment else rewritten


def update_version_in_file(var_name: str, new_version: str) -> bool:
    """Rewrite a pin in the vars file, keeping its formatting and comment.

    False when the key is absent or the pin is digest-locked elsewhere.
    """
    # version_file entries are digest-locked outside the vars file: flag the
    # update but never auto-rewrite the @sha256 pin — bumping a supply-chain
    # pinned image is a reviewed manual step.
    pinned_svc = next(
        (s for s in SERVICE_REGISTRY
         if s.get("var_name") == var_name and s.get("version_file")),
        None,
    )
    if pinned_svc:
        vf = pinned_svc["version_file"]
        if vf == "ci":
            where = ".gitlab-ci.yml"
        else:
            where = ", ".join(vf) if isinstance(vf, list) else vf
        print(
            f"  ↳ {pinned_svc['name']} is digest-pinned in {where} — update "
            f"manually: bump the tag to {new_version} and re-pin its @sha256 "
            f"digest (supply-chain pin, not auto-rewritten)."
        )
        return False

    content = VARS_FILE.read_text()
    lines = content.split("\n")
    modified = False

    if var_name.startswith("helm_chart_versions."):
        chart_key = var_name.split(".", 1)[1]
        in_helm = False
        for i, line in enumerate(lines):
            if line.strip() == "helm_chart_versions:":
                in_helm = True
                continue
            if in_helm and line.startswith("  ") and line.strip().startswith(f"{chart_key}:"):
                lines[i] = _rewrite_pin_line(line, chart_key, new_version, always_quote=True)
                modified = True
                break
            if in_helm and not line.startswith(" ") and line.strip() and not line.strip().startswith("#"):
                break
    else:
        for i, line in enumerate(lines):
            # Column-0 anchor: an indented key of the same name belongs to
            # another mapping and rewriting it would de-nest it.
            if line.startswith(f"{var_name}:"):
                lines[i] = _rewrite_pin_line(line, var_name, new_version, always_quote=False)
                modified = True
                break

    if modified:
        VARS_FILE.write_text("\n".join(lines))

    return modified


# --- Main logic ---

# category -> (report heading, fetcher name). One table drives check_service's
# dispatch, the report groups and the --category choices; "manual" has no
# fetcher, and a registry entry outside the map renders under "Other".
CATEGORIES: dict[str, tuple[str, str]] = {
    "github": ("GitHub Releases", "fetch_github_release"),
    "dockerhub": ("Container Images (Docker Hub)", "fetch_dockerhub_version"),
    "ghcr": ("Container Images (GHCR)", "fetch_ghcr_version"),
    "lsio": ("Container Images (LinuxServer.io)", "fetch_lsio_version"),
    "helm": ("Helm Charts", "fetch_helm_version"),
    "apt_repo": ("APT Repositories (upstream)", "fetch_apt_repo_version"),
    "manual": ("Manual / APT Managed", ""),
}


def is_actionable(result: "ServiceVersion") -> bool:
    """An update a consumer can act on: not errored, held or current-unreadable."""
    return (
        result.update_available
        and not result.error
        and not result.held
        and not result.unreadable_current
    )


def fetcher_for(category: str) -> Optional[Callable[[dict], str]]:
    """The fetcher for `category`, resolved by name at call time."""
    _label, name = CATEGORIES.get(category, ("", ""))
    return globals().get(name) if name else None


def _annotate_latest_resolution(result: ServiceVersion, current: str) -> None:
    """When a service tracks 'latest', surface the resolved version in the notes
    so the table shows it on both the cache-hit and live-fetch paths."""
    if current == "latest" and result.latest_version:
        suffix = f"'latest' resolves to {result.latest_version}"
        result.notes = (result.notes + " " + suffix) if result.notes else suffix


def check_service(svc_def: dict, current_versions: dict[str, str], use_cache: bool = True) -> ServiceVersion:
    """Check a single service for available updates."""
    name = svc_def["name"]
    var_name = svc_def["var_name"]
    category = svc_def["category"]
    current = current_versions.get(var_name, "unknown")
    notes = svc_def.get("notes", "")

    result = ServiceVersion(
        name=name,
        category=category,
        current_version=current,
        var_name=var_name,
        notes=notes,
        held=bool(svc_def.get("held", False)),
        unreadable_current=bool(svc_def.get("unreadable_current", False)),
        coupled_vars=list(svc_def.get("coupled_vars") or []),
        revision_var=str(svc_def.get("revision_var") or ""),
        sha_var=dict(svc_def.get("sha_var") or {}),
    )

    if current == "unknown" and var_name in UNRESOLVED_PINS:
        result.error = (
            f"{var_name}: no version_file resolved a current version "
            f"(tried {', '.join(UNRESOLVED_PINS[var_name])}); the pin is no "
            "longer tracked"
        )
        return result

    if "github_repo" in svc_def:
        result.source_url = f"https://github.com/{svc_def['github_repo']}/releases"
        result.release_url = result.source_url
    elif "docker_image" in svc_def:
        result.source_url = f"https://hub.docker.com/r/{svc_def['docker_image']}/tags"
        result.release_url = result.source_url
    elif "ghcr_image" in svc_def:
        # owner/name form resolves for both user- and org-owned packages;
        # github.com/orgs/<owner>/packages 404s for user-owned ones.
        owner, _, name = svc_def["ghcr_image"].partition("/")
        result.source_url = f"https://github.com/{owner}/{name}/pkgs/container/{name}"
    if svc_def.get("source_url"):
        result.source_url = svc_def["source_url"]

    if category == "manual":
        result.latest_version = current
        result.notes = notes or "Manual version management"
        return result

    if use_cache:
        cached = _read_cache(name)
        if cached:
            result.latest_version = cached
            result.update_available = (
                current != "latest"
                and cached != current
                and version_greater(cached, current)
            )
            _annotate_latest_resolution(result, current)
            return result

    result.fetched_live = True
    try:
        svc_def_with_current = svc_def.copy()
        svc_def_with_current["_current_version"] = current

        fetcher = fetcher_for(category)
        if fetcher is None:
            result.error = f"Unknown category: {category}"
            return result
        latest = fetcher(svc_def_with_current)

        result.latest_version = latest
        _write_cache(name, latest)

        if current == "latest":
            _annotate_latest_resolution(result, current)
            result.update_available = False
        elif latest != current:
            result.update_available = version_greater(latest, current)

    except RuntimeError as e:
        result.error = str(e)
    except Exception as e:
        # Include the exception type so unknown failures are diagnosable
        # without a debugger.
        result.error = f"Unexpected {type(e).__name__}: {e}"
        if os.environ.get("DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)

    return result


def check_all(
    services: Optional[list[dict]] = None,
    category_filter: Optional[str] = None,
    use_cache: bool = True,
) -> list[ServiceVersion]:
    """Check all services for available updates."""
    current_versions = read_current_versions()

    if services is None:
        services = SERVICE_REGISTRY

    if category_filter:
        services = [s for s in services if s["category"] == category_filter]
        # An unknown --category (or one that no --service matches) would
        # otherwise check nothing and report a clean run — a typo must not read
        # as "everything is up to date".
        if not services:
            raise ValueError(
                f"no services match category {category_filter!r} "
                "(check the spelling, or the --service filter combined with it)"
            )

    results = []
    for svc_def in services:
        result = check_service(svc_def, current_versions, use_cache=use_cache)
        results.append(result)
        # Small delay between live API calls to be nice to rate limits.
        # Skip it on cache hits / manual services (no network call made).
        if result.fetched_live:
            time.sleep(0.2)

    return results


# --- Output formatting ---

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def should_use_color() -> bool:
    """Determine if terminal supports color output."""
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def format_table(results: list[ServiceVersion]) -> str:
    """Format results as a human-readable table."""
    use_color = should_use_color()

    def c(code: str, text: str) -> str:
        if use_color:
            return f"{code}{text}{RESET}"
        return text

    lines = []
    lines.append("")
    lines.append(c(BOLD, REPORT_TITLE))
    lines.append(c(DIM, f"Source: {VARS_FILE}"))
    lines.append(c(DIM, f"Checked: {time.strftime('%Y-%m-%d %H:%M:%S')}"))
    lines.append("")

    categories = {key: label for key, (label, _fetcher) in CATEGORIES.items()}

    # Counted over every result, not only the printed ones, so an unrecognised
    # category cannot skew the summary.
    updates_available = sum(1 for r in results if is_actionable(r))
    errors = sum(1 for r in results if r.error)

    groups = [(k, n, [r for r in results if r.category == k]) for k, n in categories.items()]
    other = [r for r in results if r.category not in categories]
    if other:
        groups.append(("other", "Other (unrecognised category)", other))

    for _cat_key, cat_name, cat_results in groups:
        if not cat_results:
            continue

        lines.append(c(BOLD + CYAN, f"--- {cat_name} ---"))
        lines.append("")

        name_w = max(len(r.name) for r in cat_results)
        cur_w = max(len(r.current_version) for r in cat_results)
        lat_w = max(len(r.latest_version or "error") for r in cat_results)

        header = f"  {'Service':<{name_w}}  {'Current':<{cur_w}}  {'Latest':<{lat_w}}  Status"
        lines.append(c(DIM, header))
        lines.append(c(DIM, "  " + "-" * (name_w + cur_w + lat_w + 20)))

        for r in cat_results:
            latest_str = r.latest_version or "error"

            if r.error:
                status = c(RED, "ERROR")
                latest_str = "?"
            elif r.unreadable_current:
                status = c(DIM, "CURRENT UNREADABLE")
            elif r.update_available and r.held:
                status = c(DIM, "HELD")
            elif r.update_available:
                status = c(YELLOW, "UPDATE AVAILABLE")
            elif r.current_version == "latest":
                status = c(DIM, "tracking latest")
            else:
                status = c(GREEN, "up to date")

            line = f"  {r.name:<{name_w}}  {r.current_version:<{cur_w}}  {latest_str:<{lat_w}}  {status}"
            lines.append(line)

            if r.notes:
                lines.append(c(DIM, f"  {'':>{name_w}}  {r.notes}"))
            if r.error:
                lines.append(c(RED, f"  {'':>{name_w}}  Error: {r.error}"))

        lines.append("")

    lines.append(c(BOLD, "--- Summary ---"))
    total = len(results)
    held = sum(1 for r in results if r.update_available and r.held and not r.error)
    unreadable = sum(1 for r in results if r.unreadable_current and not r.error)
    up_to_date = total - updates_available - held - unreadable - errors
    lines.append(f"  Total services: {total}")
    lines.append(f"  Up to date:     {c(GREEN, str(up_to_date))}")
    if updates_available > 0:
        lines.append(f"  Updates:        {c(YELLOW, str(updates_available))}")
    else:
        lines.append(f"  Updates:        {updates_available}")
    if held > 0:
        lines.append(f"  Held:           {c(DIM, str(held))} (documented holds, not actionable)")
    if unreadable > 0:
        lines.append(
            f"  Pin elsewhere:  {c(DIM, str(unreadable))} (current version not readable here)"
        )
    if errors > 0:
        lines.append(f"  Errors:         {c(RED, str(errors))}")
    else:
        lines.append(f"  Errors:         {errors}")
    lines.append("")

    if updates_available > 0:
        lines.append(c(DIM, "To update a specific service:"))
        lines.append(c(DIM, "  check-versions.py --update <name>"))
        lines.append(c(DIM, ""))
        lines.append(c(DIM, "To update all outdated services:"))
        lines.append(c(DIM, "  check-versions.py --update-all"))
        lines.append("")

    return "\n".join(lines)


def format_json(results: list[ServiceVersion]) -> str:
    """Format results as JSON.

    `updates_available` counts actionable updates only; held ones are counted
    in `updates_held` and report `update_available: false` per service.
    """
    data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_file": str(VARS_FILE),
        "services": [],
        "summary": {
            "total": len(results),
            "up_to_date": sum(
                1
                for r in results
                if not r.update_available and not r.error and not r.unreadable_current
            ),
            "updates_available": sum(1 for r in results if is_actionable(r)),
            "updates_held": sum(1 for r in results if r.update_available and r.held),
            "updates_current_unreadable": sum(1 for r in results if r.unreadable_current),
            "errors": sum(1 for r in results if r.error),
        },
    }

    for r in results:
        entry = {
            "name": r.name,
            "category": r.category,
            "var_name": r.var_name,
            "current_version": r.current_version,
            "latest_version": r.latest_version,
            "update_available": is_actionable(r),
            "source_url": r.source_url,
        }
        if r.error:
            entry["error"] = r.error
        if r.held:
            entry["held"] = True
        if r.unreadable_current:
            entry["unreadable_current"] = True
        if r.coupled_vars:
            entry["coupled_vars"] = list(r.coupled_vars)
        if r.notes:
            entry["notes"] = r.notes
        if r.release_url:
            entry["release_url"] = r.release_url
        data["services"].append(entry)

    return json.dumps(data, indent=2)


# --- CLI ---

def get_deploy_command(result: ServiceVersion) -> str:
    """How to roll out a bumped pin.

    Per-service `deploy_command` wins; a `version_file` pin gets a derived
    instruction naming those files; otherwise `default_deploy_command`.
    """
    svc = next(
        (s for s in SERVICE_REGISTRY if s.get("var_name") == result.var_name),
        None,
    )
    if svc:
        if svc.get("deploy_command"):
            return svc["deploy_command"]
        version_file = svc.get("version_file")
        if version_file:
            if isinstance(version_file, str) and version_file in VERSION_FILE_ALIASES:
                files = str(VERSION_FILE_ALIASES[version_file])
            elif isinstance(version_file, str):
                files = version_file
            else:
                files = ", ".join(version_file)
            return (
                f"edit the image tag + @sha256 digest in {files}, then commit + push"
            )
    return DEFAULT_DEPLOY_COMMAND


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. --category is validated against CATEGORIES."""
    parser = argparse.ArgumentParser(
        prog="check-versions.py",
        description="Compare pinned versions against their upstream releases.",
        epilog=(
            "Environment:\n"
            "  GITHUB_TOKEN           GitHub token for higher API rate limits\n"
            "  CHECK_VERSIONS_CONFIG  Consumer config path\n"
            "  NO_COLOR               Disable coloured output"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--service", metavar="NAME", type=str.lower,
                        help="Check services matching NAME only")
    parser.add_argument("--category", metavar="CAT", type=str.lower,
                        choices=sorted(CATEGORIES),
                        help="Check one category only (%s)"
                             % ", ".join(sorted(CATEGORIES)))
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output as JSON")
    parser.add_argument("--no-cache", action="store_true",
                        help="Skip the cache, force fresh lookups")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Clear the version cache and exit")
    parser.add_argument("--update", metavar="NAME", type=str.lower,
                        help="Update one service's pin in the vars file")
    parser.add_argument("--update-all", action="store_true",
                        help="Update every outdated pin in the vars file")
    parser.add_argument("--list", action="store_true", dest="list_services",
                        help="List all tracked services and exit")
    parser.add_argument("--check-coverage", action="store_true",
                        help="Fail if a *_version pin has no registry entry")
    parser.add_argument("--check-partner-pins", metavar="BASE_REF",
                        help="Fail if a coupled pin moved and its partner did not")
    parser.add_argument("--config", metavar="PATH",
                        help="Consumer config (default: $CHECK_VERSIONS_CONFIG, "
                             "then scripts/version-registry.{py,json})")
    parser.add_argument("--repo-root", metavar="DIR",
                        help="Root every config path resolves against")
    return parser


def _run_update(service_name: str) -> None:
    """--update: check one service live and write its pin. Always exits."""
    matched = [
        s for s in SERVICE_REGISTRY
        if s["name"].lower() == service_name
        or s["var_name"].lower() == service_name
        or s["var_name"].replace("_version", "").lower() == service_name
    ]
    if not matched:
        print(f"Error: Unknown service '{service_name}'")
        print("Run with --list to see available services")
        sys.exit(1)

    svc_def = matched[0]
    current_versions = read_current_versions()
    result = check_service(svc_def, current_versions, use_cache=False)

    if result.error:
        print(f"Error checking {result.name}: {result.error}")
        sys.exit(1)

    if not result.update_available:
        print(f"{result.name} is already at the latest version ({result.current_version})")
        sys.exit(0)

    if result.unreadable_current:
        print(f"{result.name}'s pin is not readable from this repo: "
              f"{result.notes or 'see the registry entry'}")
        print(f"Latest upstream release: {result.latest_version}. Edit the pin where it lives.")
        sys.exit(0)

    if result.held:
        print(f"{result.name} is held back: {result.notes or 'documented hold'}")
        print(f"Not updating (would write {result.latest_version} into {VARS_FILE.name}).")
        print("Remove the 'held' flag in SERVICE_REGISTRY to override.")
        sys.exit(0)

    print(f"Updating {result.name}: {result.current_version} -> {result.latest_version}")
    if update_version_in_file(result.var_name, result.latest_version):
        print(f"Updated {result.var_name} in {VARS_FILE.name}")
        try:
            write_companion_pins(result)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        if result.notes:
            print(f"  {result.notes}")
        step = 1
        print("\nNext steps:")
        if result.coupled_vars:
            step += 1
            print(f"  1. PAIRED EDIT REQUIRED: also update "
                  f"{', '.join(result.coupled_vars)} in {VARS_FILE.name}"
                  + (f" - {result.notes}" if result.notes else ""))
        print(f"  {step}. Review the change: git diff {VARS_FILE}")
        print(f"  {step + 1}. Deploy the update: {get_deploy_command(result)}")
        sys.exit(0)

    # No write means the pin was renamed or the file format changed. Fail
    # loudly so CI / the Taskfile cannot read it as success.
    print(f"ERROR: Could not find {result.var_name} in {VARS_FILE.name}", file=sys.stderr)
    sys.exit(1)


def _run_update_all() -> None:
    """--update-all: write every actionable pin. Always exits."""
    results = check_all(use_cache=False)
    updated = []
    write_failed = []
    errored = [r for r in results if r.error]
    held_skipped = [r for r in results if r.update_available and r.held and not r.error]
    unreadable_skipped = [r for r in results if r.unreadable_current and not r.error]
    # A coupled pin IS written; its partner value (a checksum, a git SHA) is a
    # hand edit this script cannot compute, so it is flagged loudly instead.
    # --check-partner-pins reds the MR while a partner is still stale.
    paired = []
    for r in results:
        if is_actionable(r):
            print(f"Updating {r.name}: {r.current_version} -> {r.latest_version}")
            if update_version_in_file(r.var_name, r.latest_version):
                try:
                    write_companion_pins(r)
                except RuntimeError as exc:
                    print(f"  ERROR: {exc}")
                    write_failed.append(r)
                    continue
                updated.append(r)
                if r.coupled_vars:
                    paired.append(r)
                if r.notes:
                    print(f"  {r.notes}")
            else:
                print(f"  ERROR: Could not find {r.var_name} in {VARS_FILE.name}")
                write_failed.append(r)

    # Failures print before the success list so a long update run cannot bury
    # them.
    if write_failed:
        print(f"\nERROR: {len(write_failed)} service(s) could not be updated in {VARS_FILE.name}:")
        for r in write_failed:
            print(f"  - {r.var_name}")

    if errored:
        print(f"\nWARNING: {len(errored)} service(s) had errors and were NOT checked:")
        for r in errored:
            print(f"  - {r.name}: {r.error}")

    if held_skipped:
        print(f"\nNOTE: {len(held_skipped)} update(s) intentionally held back (not written):")
        for r in held_skipped:
            print(f"  - {r.name}: {r.current_version} -> {r.latest_version} "
                  f"({r.notes or 'documented hold'})")

    if unreadable_skipped:
        print(f"\nNOTE: {len(unreadable_skipped)} pin(s) live outside this repo and were not written:")
        for r in unreadable_skipped:
            print(f"  - {r.name}: upstream {r.latest_version} "
                  f"({r.notes or 'see the registry entry'})")

    if paired:
        print("\n" + "=" * 72)
        print(f"PAIRED-EDIT-REQUIRED: {len(paired)} pin(s) were written whose partner")
        print("value must be recomputed by hand before this change can merge:")
        for r in paired:
            print(f"  - {r.var_name} -> {r.latest_version}; also update "
                  f"{', '.join(r.coupled_vars)} in {VARS_FILE.name}"
                  + (f" - {r.notes}" if r.notes else ""))
        print("`--check-partner-pins <base-ref>` fails while a partner is stale.")
        print("=" * 72)

    if updated:
        print(f"\nUpdated {len(updated)} services in {VARS_FILE.name}")

        deploy_commands = {}
        for r in updated:
            deploy_commands.setdefault(get_deploy_command(r), []).append(r.name)

        print("\nNext steps:")
        print("  1. Review changes:")
        print(f"     git diff {VARS_FILE}")
        print("\n  2. Deploy updates (in this order):")
        for cmd, services in deploy_commands.items():
            print(f"     {cmd}")
            for svc in services:
                print(f"       # Updates: {svc}")

        print("\n  3. Commit the change:")
        print(f"     git add {VARS_FILE} && git commit")
    elif not errored:
        print("\nAll services are up to date!")

    # 2 — something errored or could not be written; 0 — everything succeeded,
    # whether or not anything was updated.
    sys.exit(2 if (errored or write_failed) else 0)


def main():
    args = build_parser().parse_args()

    repo_root = Path(args.repo_root) if args.repo_root else None
    load_config(
        resolve_config_path(args.config, repo_root or REPO_ROOT),
        repo_root,
    )

    if args.check_partner_pins:
        _run_check_partner_pins(args.check_partner_pins)

    if args.check_coverage:
        missing = missing_registry_entries()
        if missing:
            print(
                "ERROR: version pins with no registry entry (their updates are "
                f"never reported): {missing}\n"
                "Add a registry entry, or list the pin in the config's "
                "untracked_allowlist if it has no upstream to track.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"All {len(SERVICE_REGISTRY)} tracked pins have a registry entry.")
        sys.exit(0)

    if args.clear_cache:
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            print(f"Cache cleared: {CACHE_DIR}")
        else:
            print("No cache to clear")
        sys.exit(0)

    if args.list_services:
        print("\nTracked services:\n")
        for svc in SERVICE_REGISTRY:
            cat = svc["category"]
            var = svc["var_name"]
            print(f"  {svc['name']:<25} [{cat:<10}] var: {var}")
        print()
        sys.exit(0)

    if args.update:
        _run_update(args.update)

    if args.update_all:
        _run_update_all()

    use_cache = not args.no_cache
    service_filter = args.service
    category_filter = args.category

    services = SERVICE_REGISTRY
    if service_filter:
        services = [
            s for s in services
            if service_filter in s["name"].lower()
            or service_filter in s["var_name"].lower()
        ]
        if not services:
            print(f"Error: No services matching '{service_filter}'")
            print("Run with --list to see available services")
            sys.exit(1)

    try:
        results = check_all(services=services, category_filter=category_filter, use_cache=use_cache)
    except ValueError as exc:
        # 2, not 1: exit 1 is "updates available", which a wrapper acts on.
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json_output:
        print(format_json(results))
    else:
        print(format_table(results))

    # Exit code: 0 = all up to date, 1 = updates available, 2 = errors
    has_errors = any(r.error for r in results)
    has_updates = any(is_actionable(r) for r in results)
    if has_errors:
        sys.exit(2)
    elif has_updates:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
