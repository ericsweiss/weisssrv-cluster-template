#!/usr/bin/env python3
"""check-versions.py - Automated version discovery for a pinned-versions repo.

Checks the latest available versions from official sources and compares them
against the versions pinned in a consumer vars file (conventionally
ansible/inventories/prod/group_vars/all.yml).

Supports:
  - GitHub releases (binary tools, container images with GitHub releases)
  - Docker Hub / ghcr.io / LinuxServer.io container image tags
  - Helm chart versions from OCI/HTTP repositories
  - APT package versions from live repo indexes

WHAT to track is consumer data: the service registry, the vars file, and the
per-service deploy command live in a config file (see load_config), not here.
Resolution order: --config, then $CHECK_VERSIONS_CONFIG, then
scripts/version-registry.{py,json} under the repo root.

Usage:
  ./check-versions.py                     # check all services
  ./check-versions.py --service gluetun   # check a single service
  ./check-versions.py --category helm     # check a category
  ./check-versions.py --json              # JSON output
  ./check-versions.py --update gluetun    # update the pin in the vars file
  ./check-versions.py --update-all        # update every outdated pin
  ./check-versions.py --check-coverage    # every *_version pin has a registry entry

Environment:
  GITHUB_TOKEN / GH_API_TOKEN - optional token for higher GitHub rate limits
                 (unauthenticated: 60 req/hr, authenticated: 5000 req/hr)
  CHECK_VERSIONS_CONFIG - config path (overridden by --config)
"""

import functools
import gzip
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_CANDIDATES = ("scripts/version-registry.py", "scripts/version-registry.json")

# All five are populated by load_config(); see its docstring for the schema.
SERVICE_REGISTRY: list[dict] = []
VARS_FILE: Path = REPO_ROOT / "ansible/inventories/prod/group_vars/all.yml"
# Aliases for `version_file` values that are not repo-relative paths, e.g.
# {"ci": ".gitlab-ci.yml"} for digest-locked `image:` pins in the CI file.
VERSION_FILE_ALIASES: dict[str, Path] = {}
DEFAULT_DEPLOY_COMMAND = ""
UNTRACKED_ALLOWLIST: set[str] = set()

CACHE_DIR = REPO_ROOT / ".version-cache"
CACHE_TTL = 3600  # 1 hour cache

# GitHub API rate limit handling
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GH_API_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")

# Request timeout in seconds
REQUEST_TIMEOUT = 15

# Bounded retry for transient network failures. The checker does dozens of
# sequential external fetches (GitHub, Docker Hub, LSIO, Helm, apt); without a
# retry a single flaky endpoint (DNS blip, connection reset, upstream 5xx) makes
# the whole CI version check fail intermittently. We retry only on transient
# failures (URLError, socket.timeout, HTTP 5xx) — never on 4xx (including a 403
# rate-limit, which is surfaced as-is so it isn't masked as a transient blip).
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
    # A held update is reported but not actionable: it doesn't flip the
    # exit code or trigger MR comments (e.g. MetalLB 0.16.x blocked on an
    # open upstream regression). The registry entry documents why in notes.
    held: bool = False
    # True only when this check performed a live network fetch (not a cache
    # hit or a manual/no-check service). Lets check_all skip the rate-limit
    # sleep on cache hits.
    fetched_live: bool = False


# ---------------------------------------------------------------------------
# Consumer config
# ---------------------------------------------------------------------------

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

    The Python form exists so a consumer's registry keeps its inline rationale
    comments (a JSON registry loses every "why we pin this" note) — it is repo
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


def load_config(path: Path, repo_root: Optional[Path] = None) -> dict:
    """Populate the module-level config globals from a consumer config file.

    Schema (JSON object, or a .py module defining CONFIG / SERVICE_REGISTRY):

        {
          "vars_file": "ansible/inventories/prod/group_vars/all.yml",
          "cache_dir": ".version-cache",
          "default_deploy_command": "task infra:deploy",
          "version_file_aliases": {"ci": ".gitlab-ci.yml"},
          "untracked_allowlist": ["debian_version"],
          "services": [
            {"name": "k3s", "var_name": "k3s_version", "category": "github",
             "github_repo": "k3s-io/k3s", "version_prefix": "v",
             "deploy_command": "task maintenance:update-k3s-nodes"}
          ]
        }

    Every path is resolved against `repo_root` (default: the script's repo).
    """
    global SERVICE_REGISTRY, VARS_FILE, VERSION_FILE_ALIASES, CACHE_DIR
    global DEFAULT_DEPLOY_COMMAND, UNTRACKED_ALLOWLIST, REPO_ROOT

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
    return cfg


def missing_registry_entries() -> list[str]:
    """`*_version` pins present in the vars file with no registry entry.

    An untracked pin is silently never reported as outdated, so `--check-coverage`
    turns that into a CI failure. Pins with no upstream to track go in the
    config's `untracked_allowlist`.
    """
    tracked = {s["var_name"] for s in SERVICE_REGISTRY}
    return sorted(
        v for v in read_current_versions()
        if (v.endswith("_version") or v.startswith("helm_chart_versions."))
        and v not in tracked
        and v not in UNTRACKED_ALLOWLIST
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _urlopen_with_retry_full(req, timeout: int = REQUEST_TIMEOUT) -> tuple[str, bytes]:
    """urlopen with a bounded retry on transient failures; return (content_type, body).

    Retries on URLError, socket.timeout, and HTTP 5xx (transient upstream
    errors). HTTP 4xx — including a 403 rate-limit — is re-raised immediately
    so callers can surface it as-is rather than masking it as a transient blip.
    After RETRY_ATTEMPTS, the last exception is re-raised unchanged, so callers
    behave identically to the no-retry version once retries are exhausted.

    The Content-Type header is returned alongside the body so callers that
    must distinguish a real payload from an HTML error page (fetch_apt_packages)
    keep that sniffing logic while still going through the retry helper.
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
            # Mid-body truncation (GitHub intermittently cuts large release
            # payloads — observed as IncompleteRead on the ~25MB Codex asset
            # list). Transient: the next attempt re-reads the full body.
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
            # Check for rate limiting
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
    """Fetch apt Packages file, trying uncompressed first then .gz fallback.

    Some apt repositories only provide compressed Packages.gz files.
    This function handles both cases for better reliability.

    Args:
        base_url: URL to the Packages file (without .gz extension)

    Returns:
        The contents of the Packages file as a string

    Raises:
        RuntimeError: If neither Packages nor Packages.gz can be fetched
    """
    req_headers = {"User-Agent": "weisssrv-lib-version-check/1.0"}

    def _is_valid_packages_response(content_type: str, content: str) -> bool:
        """Check if response is a valid Packages file (not an HTML error page)."""
        # Packages files are text/plain or have no Content-Type
        # HTML error pages will have text/html
        if "text/html" in content_type.lower():
            return False
        # Also check content for HTML markers in case Content-Type is missing
        if content.strip().startswith("<!DOCTYPE") or content.strip().startswith("<html"):
            return False
        # Valid Packages files contain "Package:" lines
        if "Package:" not in content:
            return False
        return True

    # Try uncompressed first. Route through the bounded retry helper so a
    # transient 5xx/timeout retries (same protection Tailscale's fetcher gets);
    # the helper returns the Content-Type so the HTML-vs-payload sniff below is
    # preserved.
    try:
        req = urllib.request.Request(base_url, headers=req_headers)
        content_type, raw = _urlopen_with_retry_full(req, timeout=REQUEST_TIMEOUT)
        content = raw.decode("utf-8")
        if content.strip() and _is_valid_packages_response(content_type, content):
            return content
    except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, UnicodeDecodeError):
        pass

    # Fall back to .gz compressed version
    gz_url = f"{base_url}.gz"
    req = urllib.request.Request(gz_url, headers=req_headers)

    try:
        content_type, compressed_data = _urlopen_with_retry_full(req, timeout=REQUEST_TIMEOUT)
        # Check Content-Type before attempting decompression
        if "text/html" in content_type.lower():
            raise RuntimeError(f"Received HTML error page instead of Packages.gz from {gz_url}")

        with gzip.GzipFile(fileobj=BytesIO(compressed_data)) as gz:
            content = gz.read().decode("utf-8")
            # Validate the decompressed content
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


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------

def parse_version_tuple(version_str: str) -> tuple:
    """Parse a version string into a comparable tuple.

    Handles formats like:
      1.2.3, v1.2.3, 2025.12.3, 1.2.3.4567, v1.33.7+k3s1, v1.35.2+k3s10

    Numeric suffixes (like k3s1, k3s10) are handled by extracting all numeric
    parts for proper ordering (so k3s10 > k3s9, not k3s10 < k3s9).

    Returns a tuple of (type_rank, value) pairs where type_rank is 0 for ints
    and 1 for strings. This ensures consistent comparison ordering: all ints
    sort before all strings, and within each type, values compare naturally.
    """
    # Remove leading 'v' for comparison
    v = version_str.lstrip("v")
    # Drop a Debian epoch prefix (e.g. "1:1.80.0" -> "1.80.0") so the epoch
    # integer is not parsed as a leading version segment.
    epoch_match = re.match(r"^\d+:(.*)$", v)
    if epoch_match:
        v = epoch_match.group(1)
    # Replace + with . for k3s-style versions
    v = v.replace("+", ".")
    # Split on . and - and try to convert to ints
    parts = re.split(r"[.\-]", v)
    result = []
    for part in parts:
        # Split part into alternating text/numeric segments for proper ordering
        # This handles both "123abc" and "abc123" patterns (e.g., "k3s1", "k3s10")
        segments = re.findall(r"(\d+|\D+)", part)
        for seg in segments:
            if seg.isdigit():
                # Tuple of (type_rank=0, int_value) - ints sort before strings
                result.append((0, int(seg)))
            else:
                # Tuple of (type_rank=1, str_value) - strings sort after ints
                result.append((1, seg))
    return tuple(result)


def version_tuple_greater(a: tuple, b: tuple) -> bool:
    """Compare two version tuples, handling different lengths correctly.

    This handles the case where versions have different segment counts:
    - "17.1" > "17" (17.1 is newer - more segments with matching prefix)
    - "17.1-trixie" > "17-trixie" (17.1 is newer)
    - "18-trixie" > "17.1-trixie" (18 is newer)

    The key insight: when comparing version segments at the same position,
    a numeric segment (like a minor version number) takes precedence over
    a string segment (like a suffix). This handles the case where:
    - "17.1-trixie" ((0,17),(0,1),(1,"trixie")) vs "17-trixie" ((0,17),(1,"trixie"))
    - At index 1: (0,1) vs (1,"trixie") - numeric vs string

    Comparison rules for (type_rank, value) tuples at same position:
    - Same type_rank: compare values normally
    - Different type_rank: numeric (0) beats string (1) for version purposes
      because a numeric segment represents a version number, not a suffix

    Returns True if tuple a represents a newer version than tuple b.
    """
    # Compare element by element up to the shorter length
    min_len = min(len(a), len(b))
    for i in range(min_len):
        a_elem, b_elem = a[i], b[i]
        a_type, a_val = a_elem
        b_type, b_val = b_elem

        # Same type: compare values
        if a_type == b_type:
            if a_val > b_val:
                return True
            if a_val < b_val:
                return False
            # Equal, continue to next element
        else:
            # Different types: numeric (0) beats string (1)
            # This handles "17.1-trixie" vs "17-trixie" at position 1:
            #   (0, 1) [numeric minor version] vs (1, "trixie") [string suffix]
            #   Numeric segment = more specific version = newer
            return a_type < b_type  # 0 < 1, so numeric wins

    # All compared elements are equal; now check remaining elements
    if len(a) == len(b):
        return False  # Identical versions

    # Versions have different lengths with matching prefix
    # The longer version is newer IF its next element is numeric (type_rank=0)
    # Examples:
    #   "17.1" ((0,17),(0,1)) > "17" ((0,17)) - extra numeric = newer
    #   "17-alpha" ((0,17),(1,"alpha")) < "17" ((0,17)) - extra string suffix = older (pre-release)
    if len(a) > len(b):
        # a has more segments - a is newer if next segment is numeric
        return a[min_len][0] == 0  # type_rank 0 = numeric
    else:
        # b has more segments - b is newer if next segment is numeric, so a is NOT newer
        return b[min_len][0] != 0  # a is newer only if b's extra is a string (pre-release)


def version_greater(a: str, b: str) -> bool:
    """Return True if version a is greater than version b.

    Uses version_tuple_greater for proper handling of versions with different
    segment counts (e.g., "17.1-trixie" > "17-trixie").
    """
    try:
        return version_tuple_greater(parse_version_tuple(a), parse_version_tuple(b))
    except (TypeError, ValueError):
        # Fallback to lexicographic — log so operators know comparison quality may be degraded
        print(f"Warning: falling back to string comparison for {a!r} vs {b!r}", file=sys.stderr)
        return a > b


def version_compare(a: str, b: str) -> int:
    """Compare two version strings for sorting.

    Returns:
        -1 if a < b, 0 if a == b, 1 if a > b

    This is a comparator function suitable for use with functools.cmp_to_key.
    Uses semantic comparison via parsed tuples, so "1.0.0" and "v1.0.0" are equal.
    """
    # Parse both versions to tuples for semantic comparison
    a_tuple = parse_version_tuple(a)
    b_tuple = parse_version_tuple(b)

    # If tuples are equal, versions are semantically identical
    if a_tuple == b_tuple:
        return 0
    if version_tuple_greater(a_tuple, b_tuple):
        return 1
    return -1


# ---------------------------------------------------------------------------
# Version fetchers
# ---------------------------------------------------------------------------

def _debian_version_part_compare(a: str, b: str) -> int:
    """Compare one Debian upstream_version or debian_revision part per
    debian-policy §5.6.12: alternate non-digit and digit chunks. Non-digit
    chunks compare lexically with the tweak that letters sort before all
    non-letters and `~` sorts before everything (including the empty
    string); digit chunks compare numerically.
    """
    def order(c: str) -> int:
        # Sort key inside a non-digit chunk:
        #   '~'  → -1   (sorts before end-of-string and before everything else)
        #   ''   →  0   (end of chunk, before any non-tilde char)
        #   letter → ord (a..z, A..Z) — sorts before non-letter non-tilde
        #   other → ord + 256  (sorts after letters)
        if c == "~":
            return -1
        if c == "":
            return 0
        if c.isalpha():
            return ord(c)
        return ord(c) + 256

    i = j = 0
    while i < len(a) or j < len(b):
        # Non-digit run
        sa = ""
        while i < len(a) and not a[i].isdigit():
            sa += a[i]
            i += 1
        sb = ""
        while j < len(b) and not b[j].isdigit():
            sb += b[j]
            j += 1
        # Compare character by character with Debian's ordering tweaks
        for k in range(max(len(sa), len(sb))):
            ca = sa[k] if k < len(sa) else ""
            cb = sb[k] if k < len(sb) else ""
            if order(ca) != order(cb):
                return -1 if order(ca) < order(cb) else 1

        # Digit run — compare numerically (skipping leading zeros)
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
    """Compare two Debian package version strings per debian-policy
    §5.6.12 (epoch:upstream_version[-debian_revision]). Returns -1, 0, +1.

    Reimplemented in pure Python so the controller doesn't need dpkg
    installed (e.g. when run from a macOS dev machine). The ordering rules
    below are asserted in test_check_versions.py (TestDebianVersionCompare):
      0:1.98.4 < 1.98.5
      1:0.4.6-1 > 0.4.6 (epoch wins)
      0.5.0~rc1-1 < 0.5.0-1 (tilde is pre-release)
      0.4.6-1ubuntu1 > 0.4.6-1 (revision tail)
    """
    # Split epoch. Per debian-policy §5.6.12 the epoch is "a single
    # (generally small) unsigned integer". Anything else with a `:` in it
    # is malformed and we raise rather than silently dropping back to
    # epoch=0 (which would otherwise hide upstream metadata bugs as
    # "version unchanged" reports). The `:` itself is reserved as the
    # epoch separator so there's no legitimate non-epoch case to fall
    # back to.
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

    Packages files are blank-line-separated stanzas, each with a `Package:` and
    a `Version:` line. Returns the version of every stanza whose Package matches;
    callers pick their own comparator (debian_version_compare vs the
    parse_version_tuple family) and any pre-release filtering on the result.
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


def fetch_apt_repo_version(svc: dict) -> str:
    """Fetch latest version from a Debian apt repo's Packages index.

    Use for upstream-managed apt repos (e.g. pkgs.tailscale.com) where the
    GitHub release cadence runs ahead of the apt publish cadence. Tracking
    GitHub would advertise versions that `apt-get install` can't satisfy.

    Required keys in `svc`:
      apt_url:       URL to the (typically gzipped) Packages file (alias:
                     apt_index_url), e.g.
                     https://pkgs.tailscale.com/stable/debian/dists/trixie/main/binary-amd64/Packages.gz
                     Detect gzip from the response payload header rather
                     than the URL suffix — apt mirrors often serve the
                     index with a redirect and/or the `.gz` may be
                     stripped in the final URL.
      apt_package:   Binary package name (e.g. "tailscale").
    """
    # `apt_url` is the name the published schema and the shipped example use;
    # `apt_index_url` is the older spelling this function was written against.
    url = svc.get("apt_url") or svc["apt_index_url"]
    pkg = svc["apt_package"]
    req = urllib.request.Request(url, headers={"User-Agent": "weisssrv-lib-version-check/1.0"})
    # Bounded retry on transient failures (see _urlopen_with_retry).
    raw = _urlopen_with_retry(req, timeout=30)
    # gzip magic bytes are 0x1f 0x8b. Sniff the payload rather than the
    # URL extension so an apt-mirror redirect that drops `.gz` from the
    # path (or one that serves un-gzipped content over a `.gz` URL)
    # parses correctly.
    text = (
        gzip.decompress(raw).decode("utf-8", errors="replace")
        if raw[:2] == b"\x1f\x8b"
        else raw.decode("utf-8", errors="replace")
    )

    # Collect every Version line for our target package, then return the
    # highest using debian-policy version ordering (epochs, revisions, and `~`
    # pre-release semantics — a plain string-tuple compare would silently get
    # these wrong).
    versions = _collect_apt_versions(text, pkg)
    if not versions:
        raise RuntimeError(f"package '{pkg}' not found in {url}")
    latest = versions[0]
    for v in versions[1:]:
        if debian_version_compare(v, latest) > 0:
            latest = v
    return latest


def fetch_github_release(svc: dict) -> str:
    """Fetch latest release version from GitHub.

    When tag_filter is specified, collects all matching releases and returns
    the one with the highest version number (not the most recently published).
    This handles projects like Authentik that maintain multiple release branches
    and may publish patches to older branches after newer releases.

    Pagination: GitHub returns max 100 releases per page. For repos with many
    releases, we paginate up to 5 pages (500 releases) to ensure we find all
    matching versions.
    """
    repo = svc["github_repo"]
    tag_filter = svc.get("tag_filter")
    prefix = svc.get("version_prefix", "")
    strip_prefix = svc.get("strip_prefix", False)

    if tag_filter:
        # List releases and filter, then sort by version to get the highest
        # Use per_page=100 (GitHub API maximum) and paginate to avoid missing
        # versions in repos with many releases across multiple branches
        matching_versions = []
        max_pages = 5  # Limit pagination to avoid excessive API calls

        for page in range(1, max_pages + 1):
            releases = github_api(f"/repos/{repo}/releases?per_page=100&page={page}")

            # Empty page means we've exhausted all releases
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

            # If we got fewer than 100 releases, this is the last page
            if len(releases) < 100:
                break

        if not matching_versions:
            raise RuntimeError(f"No release matching {tag_filter}")

        # Sort by semantic version comparison to get the highest version, not the most recent by date
        # Uses version_compare for proper handling of mixed numeric/string segments
        matching_versions.sort(key=functools.cmp_to_key(version_compare), reverse=True)
        return matching_versions[0]
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
) -> Optional[str]:
    """Highest Docker Hub tag of `image` matching `regex` (group 1 = version).

    Shared by fetch_dockerhub_version and fetch_lsio_version. With
    return_full_tag the original tag name is returned (what the pins store);
    otherwise the captured version group is returned. version_prefix narrows both
    the API query (Docker Hub `name=` filter) and the accepted tags (startswith)
    to a release series; pin_major + current confine results to current's major.
    name_filter narrows ONLY the API query (substring match, no startswith
    constraint) — needed for suffix-style tag families like python's `-slim`,
    which otherwise scroll off the last_updated page behind other variants.
    Returns None if nothing matches; raises on a non-JSON response.
    """
    # For postgres, use larger page size to find alpine/trixie tags.
    # For version_prefix-pinned services, use Docker Hub's name= filter so
    # old tags that have scrolled off the first page are still found.
    page_size = 100 if image == "library/postgres" else 50
    url = f"https://hub.docker.com/v2/repositories/{image}/tags?page_size={page_size}&ordering=last_updated"
    if name_filter:
        url += f"&name={name_filter}"
    elif version_prefix:
        url += f"&name={version_prefix}"
    # Bounded pagination: high-churn repos (the *arr apps push develop/nightly
    # tags daily, 3 arch variants each) can bury a monthly stable tag beyond the
    # first page even with a name filter — observed 2026-07-19 when Prowlarr's
    # stable scrolled off and the check errored. Callers with that exposure pass
    # max_pages > 1; the default keeps every other caller at one request.
    results = []
    pages = 0
    while url and pages < max_pages:
        data = _make_request(url)
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected non-JSON response from {url}")
        results.extend(data.get("results", []))
        url = data.get("next")
        pages += 1

    # Extract the major version from `current` when pinning (e.g. "17-trixie"
    # -> "17", "17.2-trixie" -> "17", "v1.2.3" -> "1"). Tolerate a leading "v" so
    # v-prefixed schemes (k3s, gluetun, redis-exporter, ...) aren't silently
    # un-pinned.
    major_filter = None
    if pin_major and current:
        m = re.match(r"^v?(\d+)", current)
        if m:
            major_filter = m.group(1)

    best = None
    best_tuple = None
    for result in results:
        tag_name = result.get("name", "")
        match = re.match(regex, tag_name)
        if not match:
            continue
        # version_prefix: only consider tags starting with this prefix
        # (e.g. "v1.15." restricts to patch updates within 1.15.x).
        if version_prefix and not tag_name.startswith(version_prefix):
            continue
        # Compare/filter on the CAPTURED version (group 1), not the raw tag: a
        # leading "v" (or a regex prefix before the digits) must not bypass the
        # major pin or wrongly reject valid same-major tags. A tag_regex with
        # no capture group (the common shape, and what the shipped examples
        # use) compares the whole tag instead of raising IndexError.
        extracted = match.group(1) if match.lastindex else match.group(0)
        if major_filter:
            tag_major = re.match(r"^v?(\d+)", extracted)
            if not tag_major or tag_major.group(1) != major_filter:
                continue  # Skip tags from a different major version
        try:
            vtuple = parse_version_tuple(extracted)
        except (TypeError, ValueError):
            continue
        if best_tuple is None or version_tuple_greater(vtuple, best_tuple):
            best_tuple = vtuple
            best = tag_name if return_full_tag else extracted
    return best


def fetch_dockerhub_version(svc: dict) -> str:
    """Fetch latest version from Docker Hub using tag_regex.

    tag_regex MAY carry a capture group for the version portion (the value is
    then the captured text); with no group the whole matching tag is used.
    The highest matching version (by version tuple comparison) is returned as
    the full tag name (that is what the pins store).

    If pin_major_version is True, only returns versions matching the same major
    version as the current version.
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
    )
    if best_tag is None:
        raise RuntimeError(f"No matching tags found for {image} (regex: {tag_regex})")
    return best_tag


def fetch_lsio_version(svc: dict) -> str:
    """Fetch latest version from LinuxServer.io Docker Hub images.

    LinuxServer.io images use canonical version tags with prefixes:
      version-vX.Y.Z (nzbget), version-X.Y.Z-rN (qbittorrent),
      version-X.Y.Z.BUILD (*arr apps - stable branch)

    The regex captures the version portion from the tag, which is returned.
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
    """Fetch latest version tag from GitHub Container Registry.

    Uses the registry's anonymous pull-token flow plus the standard Docker
    Registry HTTP API tags/list endpoint. This works for public packages
    without a GITHUB_TOKEN — the GitHub packages REST API requires auth even
    for public images, which would make tokenless runs error.
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

    best_version = None
    best_tuple = None
    for tag in tags_resp.get("tags") or []:
        if not re.match(tag_filter, tag):
            continue
        try:
            vtuple = parse_version_tuple(tag)
        except (TypeError, ValueError):
            continue
        if best_tuple is None or version_tuple_greater(vtuple, best_tuple):
            best_tuple = vtuple
            best_version = tag

    if best_version is None:
        raise RuntimeError(f"No matching tags found for ghcr.io/{image}")

    return best_version


def fetch_helm_version(svc: dict) -> str:
    """Fetch latest chart version from a Helm repository index.

    Parses the index.yaml manually to avoid PyYAML dependency.
    The format is:
        entries:
          chartname:
          - apiVersion: v2
            version: X.Y.Z
          - apiVersion: v2
            version: X.Y.Z
          otherchartname:
          ...
    """
    repo_url = svc["helm_repo"]
    chart_name = svc["helm_chart"]

    index_url = f"{repo_url}/index.yaml"
    raw = _make_request(index_url)

    if not isinstance(raw, str):
        raise RuntimeError(f"Unexpected response type from {index_url}")

    # Find the chart section using a simple state machine
    lines = raw.split("\n")
    in_entries = False
    in_chart = False
    chart_indent = 0
    # Indent of the first key of each chart entry (the list-item content
    # column). The chart's own `version:` is a direct child key of the entry
    # and sits at this column; a dependency/maintainer `version:` nests deeper,
    # so pinning the match here keeps a dependency version from being collected.
    entry_key_indent = None
    versions = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Track when we enter the entries: block
        if stripped == "entries:":
            in_entries = True
            continue

        if not in_entries:
            continue

        # Detect chart name entry - it appears as "  chartname:" under entries
        # The key thing is that chart names are at a consistent indentation level
        if not in_chart:
            # Chart names are indented exactly 2 spaces under entries:
            if line.rstrip().rstrip(":") and stripped.rstrip(":") == chart_name:
                in_chart = True
                chart_indent = len(line) - len(line.lstrip())
                continue
        else:
            # Calculate this line's indent
            if not line or line.isspace():
                continue
            line_indent = len(line) - len(line.lstrip())

            # If we hit something at the same indent as the chart name
            # (another chart entry), we've exited our chart section
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

            # Capture the chart's own "version:" — a direct child key of the
            # entry (at entry_key_indent). Match on the exact key so
            # "appVersion:" is excluded and arbitrary post-colon whitespace
            # (YAML permits it) doesn't drop the line. Restricting to the entry
            # key indent skips deeper "version:" lines under a dependencies:/
            # maintainers: sub-block, which would otherwise be collected.
            if key == "version" and (entry_key_indent is None or key_indent == entry_key_indent):
                ver = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                if not re.search(r"(alpha|beta|rc|dev|snapshot)", ver, re.IGNORECASE):
                    versions.append(ver)

    if not versions:
        raise RuntimeError(f"No versions found for chart {chart_name}")

    # Sort by semantic version comparison for proper handling of mixed numeric/string segments
    versions.sort(key=functools.cmp_to_key(version_compare), reverse=True)
    return versions[0]


def fetch_plex_version(svc: dict) -> str:
    """Fetch latest Plex Media Server version from Plex apt repository.

    Queries the actual apt repository Packages file to get the version
    that's available for installation, rather than the Plex downloads API
    which may advertise versions not yet available in apt.

    Collects all plexmediaserver versions and returns the highest one,
    since the Packages file may contain multiple versions.
    """
    # Fetch from the apt repository Packages file (where apt actually installs from)
    # v2 repository URL (as of Plex v1.43.0)
    # Uses fetch_apt_packages to handle both uncompressed and .gz formats
    packages_url = "https://repo.plex.tv/deb/dists/public/main/binary-amd64/Packages"
    raw = fetch_apt_packages(packages_url)

    # The Packages file may carry multiple plexmediaserver versions; collect
    # them all (Package: plexmediaserver / Version: X.Y.Z.BUILD-hash).
    versions = _collect_apt_versions(raw, "plexmediaserver")
    if not versions:
        raise RuntimeError("Could not find plexmediaserver version in apt repository")

    # Sort by semantic version comparison for proper handling of mixed numeric/string segments
    versions.sort(key=functools.cmp_to_key(version_compare), reverse=True)
    return versions[0]


def fetch_gitlab_version(svc: dict) -> str:
    """Fetch latest GitLab EE version from GitLab apt repository.

    Queries the actual apt repository Packages file to get the version
    that's available for installation. Uses fetch_apt_packages to handle
    both uncompressed and .gz formats.
    """
    # Fetch from the apt repository Packages file (Debian trixie/bookworm amd64)
    # Try trixie first (Debian 13), fall back to bookworm (Debian 12)
    packages_urls = [
        "https://packages.gitlab.com/gitlab/gitlab-ee/debian/dists/trixie/main/binary-amd64/Packages",
        "https://packages.gitlab.com/gitlab/gitlab-ee/debian/dists/bookworm/main/binary-amd64/Packages",
    ]

    raw = None
    errors = []
    for url in packages_urls:
        try:
            raw = fetch_apt_packages(url)
            if raw and raw.strip():
                break
        except RuntimeError as e:
            errors.append(f"{url}: {e}")
            continue

    if not raw:
        raise RuntimeError(
            ("Could not fetch GitLab apt repository Packages file; attempts: "
             + "; ".join(errors)) if errors else "Could not fetch GitLab apt Packages"
        )

    # Collect every gitlab-ee Version (X.Y.Z-ee.N), skip pre-releases, and keep
    # the highest by semantic ordering with (type_rank, value) tuples.
    best_version = None
    best_tuple = None
    for version in _collect_apt_versions(raw, "gitlab-ee"):
        if re.search(r"(rc|beta|alpha)", version, re.IGNORECASE):
            continue
        try:
            vtuple = parse_version_tuple(version)
            if best_tuple is None or version_tuple_greater(vtuple, best_tuple):
                best_tuple = vtuple
                best_version = version
        except (TypeError, ValueError):
            pass

    if not best_version:
        raise RuntimeError("Could not find gitlab-ee version in apt repository")

    return best_version


# ---------------------------------------------------------------------------
# Vars-file parser (simple YAML extraction without PyYAML)
# ---------------------------------------------------------------------------

def read_pinned_image_versions() -> dict[str, str]:
    """Current tags of digest-locked `image:` pins that live outside the vars file.

    A registry entry with `version_file` is read from that file instead: an alias
    from the config's `version_file_aliases`, or one or more repo-relative
    manifest paths. Extract the tag (between ':' and the '@sha256:' digest) for
    each so a stale pin is still flagged. `image_ref` overrides the image name
    matched in the file when it differs from the API lookup name (a ghcr.io/
    registry prefix, Docker Hub's library/ namespace).
    """
    versions: dict[str, str] = {}
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
        image = svc.get("image_ref") or svc.get("docker_image", "")
        # Collect the tag from every readable path (not break-on-first) so
        # divergent pins between manifests that must share one tag are caught.
        matched: list[tuple[Path, str]] = []
        for path in paths:
            try:
                content = path.read_text()
            except OSError:
                continue
            m = re.search(
                rf"^\s*image:\s*{re.escape(image)}:([\w.+-]+?)(?:@sha256:[0-9a-f]+)?\s*$",
                content,
                re.MULTILINE,
            )
            if m:
                matched.append((path, m.group(1)))
        if not matched:
            continue
        distinct = {tag for _, tag in matched}
        if len(distinct) > 1:
            detail = ", ".join(
                f"{p.relative_to(repo_root)}={tag}" for p, tag in matched
            )
            # Fail loudly instead of silently selecting matched[0]: manifests that
            # must share one image tag have drifted, and swallowing that lets CI
            # go green with divergent pins. Raising surfaces it through the
            # blocking scripts:test unit run (which calls this on the real tree).
            raise RuntimeError(
                f"{svc['var_name']} pins diverge across manifests "
                f"that must share one tag: {detail}"
            )
        versions[svc["var_name"]] = matched[0][1]
    return versions


def read_current_versions() -> dict[str, str]:
    """Read the currently pinned versions from the vars file, without a YAML parser.

    Returns a dict mapping var_name to current version string.
    """
    content = VARS_FILE.read_text()
    versions = {}

    # Registered pins whose var_name does NOT follow the `*_version` convention
    # (e.g. lxc_template, a Proxmox appliance FILENAME rather than a semver) —
    # read those by exact top-level key match so they still resolve to a current
    # value instead of showing "unknown". version_file pins live elsewhere.
    extra_keys = {
        s["var_name"] for s in SERVICE_REGISTRY
        if s.get("var_name") and "_version" not in s["var_name"] and not s.get("version_file")
    }

    # Track if we are inside helm_chart_versions block
    in_helm = False

    for line in content.split("\n"):
        stripped = line.strip()

        # Skip comments and empty lines
        if not stripped or stripped.startswith("#"):
            continue

        # Detect helm_chart_versions block
        if stripped == "helm_chart_versions:":
            in_helm = True
            continue

        if in_helm:
            # Indented entries under helm_chart_versions
            if line.startswith("  ") and ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # Remove inline comments
                if "#" in val:
                    val = val[:val.index("#")].strip().strip('"').strip("'")
                versions[f"helm_chart_versions.{key}"] = val
            elif not line.startswith(" "):
                in_helm = False
                # Fall through to check this line as a regular entry

        _key = stripped.split(":")[0].strip()
        if not in_helm and ":" in stripped and ("_version" in _key or _key in extra_keys):
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


def update_version_in_file(var_name: str, new_version: str) -> bool:
    """Update a pin in the vars file, preserving formatting and comments.

    Returns True if the file was modified.
    """
    # version_file entries are digest-locked outside the vars file. Flag the
    # update but don't
    # auto-rewrite the @sha256 pin — bumping a supply-chain pinned image
    # should be a reviewed manual step.
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
        # Handle nested helm chart version
        chart_key = var_name.split(".", 1)[1]
        in_helm = False
        for i, line in enumerate(lines):
            if line.strip() == "helm_chart_versions:":
                in_helm = True
                continue
            if in_helm and line.startswith("  ") and line.strip().startswith(f"{chart_key}:"):
                # Preserve the comment portion
                comment = ""
                if "#" in line:
                    # Find comment after the value
                    parts = line.split("#", 1)
                    comment_text = parts[1]
                    # Update "Currently deployed" comment
                    comment_text = re.sub(
                        r"Currently deployed \S+",
                        f"Currently deployed {new_version}",
                        comment_text,
                    )
                    comment = f"# {comment_text.strip()}" if comment_text.strip() else ""

                indent = len(line) - len(line.lstrip())
                prefix = " " * indent + f'{chart_key}: "{new_version}"'
                if comment:
                    lines[i] = f"{prefix}  {comment}"
                else:
                    lines[i] = prefix
                modified = True
                break
            if in_helm and not line.startswith(" ") and line.strip() and not line.strip().startswith("#"):
                break
    else:
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{var_name}:"):
                # Preserve the comment portion
                comment = ""
                if "#" in line:
                    parts = line.split("#", 1)
                    comment_text = parts[1]
                    # Update "Currently deployed" comment
                    comment_text = re.sub(
                        r"Currently deployed \S+",
                        f"Currently deployed {new_version}",
                        comment_text,
                    )
                    comment = f"# {comment_text.strip()}" if comment_text.strip() else ""

                # Determine quoting style from original
                old_val_part = line.split(":", 1)[1]
                if "#" in old_val_part:
                    old_val_part = old_val_part[:old_val_part.index("#")]
                old_val_part = old_val_part.strip()

                uses_quotes = old_val_part.startswith('"') or old_val_part.startswith("'")

                if uses_quotes:
                    new_val = f'"{new_version}"'
                else:
                    new_val = new_version

                prefix = f"{var_name}: {new_val}"
                # Pad to align comment (rough alignment)
                if comment:
                    lines[i] = f"{prefix}  {comment}"
                else:
                    lines[i] = prefix
                modified = True
                break

    if modified:
        VARS_FILE.write_text("\n".join(lines))

    return modified


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

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
    )

    # Set source URLs
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

    # Manual/apt services - no automated check
    if category == "manual":
        result.latest_version = current
        result.notes = notes or "Manual version management"
        return result

    # Check cache first
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

    # Fetch latest version
    result.fetched_live = True
    try:
        # Add current version to svc_def for major version pinning
        svc_def_with_current = svc_def.copy()
        svc_def_with_current["_current_version"] = current

        if category == "github":
            latest = fetch_github_release(svc_def_with_current)
        elif category == "dockerhub":
            latest = fetch_dockerhub_version(svc_def_with_current)
        elif category == "lsio":
            latest = fetch_lsio_version(svc_def)
        elif category == "ghcr":
            latest = fetch_ghcr_version(svc_def)
        elif category == "helm":
            latest = fetch_helm_version(svc_def)
        elif category == "plex":
            latest = fetch_plex_version(svc_def)
        elif category == "gitlab":
            latest = fetch_gitlab_version(svc_def)
        elif category == "apt_repo":
            latest = fetch_apt_repo_version(svc_def)
        else:
            result.error = f"Unknown category: {category}"
            return result

        result.latest_version = latest
        _write_cache(name, latest)

        # Determine if update is available
        if current == "latest":
            _annotate_latest_resolution(result, current)
            result.update_available = False
        elif latest != current:
            result.update_available = version_greater(latest, current)

    except RuntimeError as e:
        result.error = str(e)
    except Exception as e:
        # Include the exception type so unknown failures ('NoneType' object
        # has no attribute 'foo') are diagnosable without re-running under
        # a debugger. Set DEBUG=1 in the environment to also print the
        # full traceback.
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


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

# ANSI colors
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
    lines.append(c(BOLD, "Homelab Version Check Report"))
    lines.append(c(DIM, f"Source: {VARS_FILE}"))
    lines.append(c(DIM, f"Checked: {time.strftime('%Y-%m-%d %H:%M:%S')}"))
    lines.append("")

    # Group by category
    categories = {
        "github": "GitHub Releases",
        "dockerhub": "Container Images (Docker Hub)",
        "ghcr": "Container Images (GHCR)",
        "lsio": "Container Images (LinuxServer.io)",
        "helm": "Helm Charts",
        "gitlab": "GitLab (packages.gitlab.com)",
        "plex": "Plex Media Server",
        "apt_repo": "APT Repositories (upstream)",
        "manual": "Manual / APT Managed",
    }

    updates_available = 0
    errors = 0

    for cat_key, cat_name in categories.items():
        cat_results = [r for r in results if r.category == cat_key]
        if not cat_results:
            continue

        lines.append(c(BOLD + CYAN, f"--- {cat_name} ---"))
        lines.append("")

        # Column widths
        name_w = max(len(r.name) for r in cat_results)
        cur_w = max(len(r.current_version) for r in cat_results)
        lat_w = max(len(r.latest_version or "error") for r in cat_results)

        # Header
        header = f"  {'Service':<{name_w}}  {'Current':<{cur_w}}  {'Latest':<{lat_w}}  Status"
        lines.append(c(DIM, header))
        lines.append(c(DIM, "  " + "-" * (name_w + cur_w + lat_w + 20)))

        for r in cat_results:
            latest_str = r.latest_version or "error"

            if r.error:
                status = c(RED, "ERROR")
                latest_str = "?"
                errors += 1
            elif r.update_available and r.held:
                status = c(DIM, "HELD")
            elif r.update_available:
                status = c(YELLOW, "UPDATE AVAILABLE")
                updates_available += 1
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

    # Summary
    lines.append(c(BOLD, "--- Summary ---"))
    total = len(results)
    held = sum(1 for r in results if r.update_available and r.held)
    up_to_date = total - updates_available - held - errors
    lines.append(f"  Total services: {total}")
    lines.append(f"  Up to date:     {c(GREEN, str(up_to_date))}")
    if updates_available > 0:
        lines.append(f"  Updates:        {c(YELLOW, str(updates_available))}")
    else:
        lines.append(f"  Updates:        {updates_available}")
    if held > 0:
        lines.append(f"  Held:           {c(DIM, str(held))} (documented holds, not actionable)")
    if errors > 0:
        lines.append(f"  Errors:         {c(RED, str(errors))}")
    else:
        lines.append(f"  Errors:         {errors}")
    lines.append("")

    if updates_available > 0:
        lines.append(c(DIM, "To update a specific service:"))
        lines.append(c(DIM, "  task maintenance:update-version SERVICE=<name>"))
        lines.append(c(DIM, ""))
        lines.append(c(DIM, "To update all outdated services:"))
        lines.append(c(DIM, "  task maintenance:update-all-versions"))
        lines.append("")

    return "\n".join(lines)


def format_json(results: list[ServiceVersion]) -> str:
    """Format results as JSON.

    Summary semantics: `updates_available` counts ACTIONABLE updates only;
    registry-held updates (held=True) are excluded and counted separately
    in `updates_held`. version-check-ci.py keys its exit code and MR
    comment off this distinction.
    """
    data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_file": str(VARS_FILE),
        "services": [],
        "summary": {
            "total": len(results),
            "up_to_date": sum(1 for r in results if not r.update_available and not r.error),
            "updates_available": sum(1 for r in results if r.update_available and not r.held),
            "updates_held": sum(1 for r in results if r.update_available and r.held),
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
            "update_available": r.update_available,
            "source_url": r.source_url,
        }
        if r.error:
            entry["error"] = r.error
        if r.held:
            entry["held"] = True
        if r.notes:
            entry["notes"] = r.notes
        if r.release_url:
            entry["release_url"] = r.release_url
        data["services"].append(entry)

    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def get_deploy_command(result: ServiceVersion) -> str:
    """How to roll out a bumped pin — registry data, with two derived fallbacks.

    Per-service `deploy_command` in the config wins. A `version_file` pin has no
    deploy step by construction (the tag + @sha256 digest are edited in place
    where the pin lives), so it gets a derived instruction naming those files.
    Anything else falls back to the config's `default_deploy_command`.
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


def print_usage():
    """Print usage information."""
    print("""Usage: check-versions.py [OPTIONS]

Options:
  --help                Show this help message
  --service NAME        Check a specific service only
  --category CAT        Check a category only (github, dockerhub, ghcr, lsio, helm, gitlab, plex, apt_repo, manual)
  --json                Output as JSON
  --no-cache            Skip cache, force fresh lookups
  --clear-cache         Clear the version cache
  --update NAME         Update a specific service's pin in the vars file
  --update-all          Update every outdated pin in the vars file
  --list                List all tracked services
  --check-coverage      Fail if a *_version pin has no registry entry
  --config PATH         Consumer config (default: $CHECK_VERSIONS_CONFIG, then
                        scripts/version-registry.{py,json})
  --repo-root DIR       Root every config path resolves against

Environment:
  GITHUB_TOKEN          GitHub personal access token for higher API rate limits
  CHECK_VERSIONS_CONFIG Consumer config path
  NO_COLOR              Disable colored output""")


def _flag_value(args: list[str], flag: str) -> Optional[str]:
    if flag not in args:
        return None
    idx = args.index(flag)
    if idx + 1 >= len(args):
        print(f"ERROR: {flag} requires a value", file=sys.stderr)
        sys.exit(2)
    return args[idx + 1]


def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print_usage()
        sys.exit(0)

    repo_root = _flag_value(args, "--repo-root")
    load_config(
        resolve_config_path(
            _flag_value(args, "--config"),
            Path(repo_root) if repo_root else REPO_ROOT,
        ),
        Path(repo_root) if repo_root else None,
    )

    if "--check-coverage" in args:
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

    if "--clear-cache" in args:
        if CACHE_DIR.exists():
            for f in CACHE_DIR.iterdir():
                f.unlink()
            print(f"Cache cleared: {CACHE_DIR}")
        else:
            print("No cache to clear")
        sys.exit(0)

    if "--list" in args:
        print("\nTracked services:\n")
        for svc in SERVICE_REGISTRY:
            cat = svc["category"]
            var = svc["var_name"]
            print(f"  {svc['name']:<25} [{cat:<10}] var: {var}")
        print()
        sys.exit(0)

    use_cache = "--no-cache" not in args
    output_json = "--json" in args
    service_filter = None
    category_filter = None

    # Parse arguments
    value_flags = ("--service", "--category", "--update", "--config", "--repo-root")
    i = 0
    while i < len(args):
        if args[i] in value_flags and i + 1 >= len(args):
            print(f"Error: {args[i]} requires an argument", file=sys.stderr)
            sys.exit(2)
        if args[i] == "--service" and i + 1 < len(args):
            service_filter = args[i + 1].lower()
            i += 2
        elif args[i] == "--category" and i + 1 < len(args):
            category_filter = args[i + 1].lower()
            i += 2
        elif args[i] == "--update" and i + 1 < len(args):
            service_name = args[i + 1].lower()
            # Find the service
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

            if result.held:
                print(f"{result.name} is held back: {result.notes or 'documented hold'}")
                print(f"Not updating (would write {result.latest_version} into {VARS_FILE.name}).")
                print("Remove the 'held' flag in SERVICE_REGISTRY to override.")
                sys.exit(0)

            print(f"Updating {result.name}: {result.current_version} -> {result.latest_version}")
            if update_version_in_file(result.var_name, result.latest_version):
                print(f"Updated {result.var_name} in {VARS_FILE.name}")
                print("\nNext steps:")
                print(f"  1. Review the change: git diff {VARS_FILE}")
                print(f"  2. Deploy the update: {get_deploy_command(result)}")
                sys.exit(0)
            else:
                # The file didn't change — var_name may have been renamed or
                # the file format changed. Fail loudly so CI / Taskfile can
                # catch it instead of silently reporting success.
                print(f"ERROR: Could not find {result.var_name} in {VARS_FILE.name}", file=sys.stderr)
                sys.exit(1)

        elif args[i] == "--update-all":
            results = check_all(use_cache=False)
            updated = []
            write_failed = []
            errored = [r for r in results if r.error]
            held_skipped = [r for r in results if r.update_available and r.held and not r.error]
            for r in results:
                if r.update_available and not r.error and not r.held:
                    print(f"Updating {r.name}: {r.current_version} -> {r.latest_version}")
                    if update_version_in_file(r.var_name, r.latest_version):
                        updated.append(r)
                    else:
                        print(f"  ERROR: Could not find {r.var_name} in {VARS_FILE.name}")
                        write_failed.append(r)

            # Surface errors FIRST — an operator looking at a long successful
            # update list could easily miss that 5 other services failed their
            # version check. Previous behavior silently swallowed errors.
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

            if updated:
                print(f"\nUpdated {len(updated)} services in {VARS_FILE.name}")

                # Group updates by deployment command
                deploy_commands = {}
                for r in updated:
                    cmd = get_deploy_command(r)
                    if cmd not in deploy_commands:
                        deploy_commands[cmd] = []
                    deploy_commands[cmd].append(r.name)

                print("\nNext steps:")
                print("  1. Review changes:")
                print(f"     git diff {VARS_FILE}")
                print("\n  2. Deploy updates (in this order):")

                # Show deployment commands with the services they update
                for cmd, services in deploy_commands.items():
                    print(f"     {cmd}")
                    for svc in services:
                        print(f"       # Updates: {svc}")

                print("\n  3. Verify deployments:")
                print("     task k3s:status")
                print("     task infra:verify")

                print("\n  4. Commit changes:")
                print("     git add -A && git commit -m 'Update service versions'")
            else:
                if not errored:
                    print("\nAll services are up to date!")
            # Exit code convention:
            #   2 — at least one service errored or couldn't be written
            #   0 — all checks succeeded (whether or not we updated anything)
            sys.exit(2 if (errored or write_failed) else 0)
        elif args[i] in ("--json", "--no-cache"):
            # Boolean flags already consumed by the `in args` checks above.
            i += 1
        elif args[i] in ("--config", "--repo-root"):
            # Value flags already consumed by _flag_value() before load_config().
            i += 2
        else:
            # Reject unknown flags loudly: a typo'd --category/--service would
            # otherwise silently run the full unfiltered check.
            print(f"Error: unknown argument '{args[i]}'", file=sys.stderr)
            print("Run with --help for usage", file=sys.stderr)
            sys.exit(2)

    # Filter services
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

    # Run checks
    results = check_all(services=services, category_filter=category_filter, use_cache=use_cache)

    # Output
    if output_json:
        print(format_json(results))
    else:
        print(format_table(results))

    # Exit code: 0 = all up to date, 1 = updates available, 2 = errors
    has_errors = any(r.error for r in results)
    has_updates = any(r.update_available and not r.held for r in results)
    if has_errors:
        sys.exit(2)
    elif has_updates:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
