#!/usr/bin/env python3
"""Point a Cloudflare zone's A records at the site's current public IPv4.

Run by a CronJob that mounts this file; configuration is environment-only and
docs/SCRIPTS.md describes it. Terraform owns every record field but `content`.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.cloudflare.com/client/v4"
# The CronJob spells this as a cluster-config placeholder Flux substitutes.
ZONE = os.environ.get("DDNS_ZONE", "").strip()

# Detection endpoints. Each reply must parse as a public IPv4 address, so an
# endpoint that answers over IPv6 or with anything else is rejected.
IP_SERVICES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
)
IP_ATTEMPTS = 3
IP_RETRY_SLEEP = 5
# Distinct providers that must report the same address before it is published,
# so one wrong or hijacked answer cannot repoint the records.
IP_QUORUM = 2


def _error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def parse_records(spec, zone=None):
    """Parse a DDNS_RECORDS spec into (records, errors).

    Entries are comma-separated `label[:proxied]`, `@` is the apex, `proxied`
    defaults to true and seeds creation only. Each bad entry yields an error."""
    zone = ZONE if zone is None else zone
    records, errors = [], []
    for entry in (e.strip() for e in spec.split(",")):
        if not entry:
            continue
        label, sep, flag = entry.partition(":")
        label, flag = label.strip(), flag.strip().lower()
        if ":" in flag:
            errors.append(f"{entry!r} has more than one ':'")
        elif sep and flag not in ("true", "false"):
            errors.append(f"{entry!r} has a proxied flag other than true or false")
        # An empty or dot-edged label builds a name like `.zone`, and a label
        # spelled as a full name builds `name.zone.zone`; the API creates either.
        elif not label or label.startswith(".") or label.endswith(".") or ".." in label:
            errors.append(f"{entry!r} has an empty or dot-edged label")
        elif zone and (label == zone or label.endswith("." + zone)):
            errors.append(f"{entry!r} is a full name where a label belongs")
        else:
            records.append((zone if label == "@" else f"{label}.{zone}", flag != "false"))
    return tuple(records), tuple(errors)


# The record list lives in cluster-config, so a WAN hostname is added there.
RECORDS, RECORD_ERRORS = parse_records(os.environ.get("DDNS_RECORDS", "").strip())


def _api_errors(data):
    """Cloudflare's `errors` array as a log suffix; empty when it names none."""
    errors = data.get("errors") if isinstance(data, dict) else None
    if not isinstance(errors, list) or not errors:
        return ""
    parts = []
    for err in errors:
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or "no message"
            parts.append(f"{code}: {message}" if code is not None else str(message))
        else:
            parts.append(str(err))
    return " (Cloudflare errors: " + "; ".join(parts) + ")"


def api_request(token, url, method="GET", data=None, timeout=30):
    """Call the Cloudflare API; return parsed JSON, or None on any failure."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Cloudflare answers with a JSON error body; keep it so the log names the cause.
        try:
            body_txt = e.read().decode(errors="replace")
        except Exception:
            body_txt = "<unreadable>"
        _error(f"HTTP {e.code} {e.reason} on {method} {url}: {body_txt}")
    except urllib.error.URLError as e:
        _error(f"network error on {method} {url}: {e.reason}")
    except Exception as e:  # noqa: BLE001 - a failed run must not crash the pod
        _error(f"unexpected {type(e).__name__} on {method} {url}: {e}")
    return None


def _fetch_ip(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode().strip()


def get_public_ip(services=IP_SERVICES, attempts=IP_ATTEMPTS, sleep=time.sleep,
                  quorum=IP_QUORUM):
    """Return the public IPv4 `quorum` distinct providers agree on, or None.

    Every reply must parse as a global IPv4, or an unreachable A record gets
    published. Retries across providers so one blip does not fail the run."""
    needed = min(quorum, len(services))
    last_err = None
    for attempt in range(1, attempts + 1):
        seen = {}
        for url in services:
            try:
                ip = _fetch_ip(url)
            except Exception as e:  # noqa: BLE001 - try the next provider
                last_err = e
                print(f"WARN: get public IP via {url} (attempt {attempt}) failed: {e}",
                      file=sys.stderr)
                continue
            try:
                if not ipaddress.IPv4Address(ip).is_global:
                    print(f"WARN: {url} returned non-public IPv4 {ip!r}, trying next",
                          file=sys.stderr)
                    continue
            except ValueError:
                if ip:
                    print(f"WARN: {url} returned non-IPv4 {ip!r}, trying next",
                          file=sys.stderr)
                continue
            seen.setdefault(ip, set()).add(url)
            if len(seen[ip]) >= needed:
                return ip
        if len(seen) > 1:
            print(f"WARN: providers disagree on the public IPv4: {sorted(seen)}",
                  file=sys.stderr)
        if attempt < attempts:
            sleep(IP_RETRY_SLEEP)
    _error(
        f"no public IPv4 that {needed} provider(s) agree on after retries: "
        f"{last_err or 'providers returned non-IPv4, empty or disagreeing answers'}"
    )
    return None


def zone_is_valid(zone):
    """False unless DDNS_ZONE holds a real, substituted domain.

    The marker is assembled at runtime because a literal dollar-brace in this
    file is a Flux envsubst parse error."""
    marker = "$" + "{"
    return bool(zone) and marker not in zone and "." in zone


def records_are_valid(records):
    """Fail closed on the record list, before the first API call.

    An empty list fails rather than exiting 0 with nothing managed; a blank name
    fails rather than sending `?type=A&name=`, which matches any record."""
    if not records:
        _error("RECORDS is empty — refusing a run that would manage no DNS at all")
        return False
    for entry in records:
        # Shape first: a two-character string unpacks as (name, proxied) and
        # would send a one-letter name to the API.
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            _error(f"record entry {entry!r} is not a (name, proxied) pair")
            return False
        name, proxied = entry
        if not isinstance(name, str) or not name.strip():
            _error(f"record entry {entry!r} has no record name — "
                   "an empty name queries the zone unfiltered")
            return False
        if not isinstance(proxied, bool):
            _error(f"record entry {entry!r} has a non-boolean proxied flag")
            return False
    return True


def get_zone_id(token, zone=None):
    """Return the Cloudflare zone id for `zone`, or None."""
    zone = ZONE if zone is None else zone
    query = urllib.parse.quote(zone, safe="")
    data = api_request(token, f"{API_BASE}/zones?name={query}")
    if not data or not data.get("success"):
        _error(f"failed to get the zone id{_api_errors(data)}")
        return None
    zones = data.get("result") or []
    # Match by NAME, never `zones[0]`: a request that degrades to an unfiltered
    # list would otherwise hand back an arbitrary zone.
    match = next((z for z in zones if z.get("name") == zone), None)
    if match is None:
        _error(f"Cloudflare returned {len(zones)} zone(s), none named {zone!r}")
        return None
    if not match.get("id"):
        _error(f"zone {zone!r} has no id in the API response")
        return None
    return match["id"]


# The PUT replaces the whole record, so a Terraform-set decoration left out of
# the body is erased on the next address change and reads as Terraform drift.
PRESERVED_FIELDS = ("comment", "tags", "settings")


def build_record_body(record_name, current_ip, proxied, existing=None):
    """Body for the full-body PUT/POST, preserving Terraform-owned fields."""
    existing = existing or {}
    body = {
        "type": "A",
        "name": record_name,
        "content": current_ip,
        "ttl": existing.get("ttl", 1),
        "proxied": existing.get("proxied", proxied),
    }
    for key in PRESERVED_FIELDS:
        # Cloudflare returns an unset decoration as null; an empty one such as
        # `tags: []` is a value the record has, so the PUT echoes it.
        if existing.get(key) is not None:
            body[key] = existing[key]
    return body


def update_record(token, zone_id, record_name, current_ip, proxied):
    """Create or update one A record. Returns True on success or no-op."""
    print(f"\n=== Processing {record_name} (create-default proxied={proxied}) ===")
    query = urllib.parse.quote(record_name, safe="")
    url = f"{API_BASE}/zones/{zone_id}/dns_records?type=A&name={query}"
    data = api_request(token, url)
    if not data or not data.get("success"):
        # A failed query reads like "no such record"; creating one would duplicate it.
        _error(f"failed to query the existing record for {record_name}{_api_errors(data)}")
        return False

    records = data.get("result") or []
    if len(records) > 1:
        # Single-record ownership is this job's contract: updating one of several
        # leaves the stale sibling answering, and picking one guesses the owner.
        _error(f"{len(records)} A records for {record_name}; "
               "ambiguous ownership, refusing a partial update")
        return False
    existing = records[0] if records else None
    existing_ip = existing.get("content") if existing else None
    record_id = existing.get("id") if existing else None
    print(f"Existing IP: {existing_ip or 'none'}")
    if current_ip == existing_ip:
        print("IP unchanged, skipping")
        return True

    body = build_record_body(record_name, current_ip, proxied, existing)
    if record_id:
        result = api_request(
            token, f"{API_BASE}/zones/{zone_id}/dns_records/{record_id}", method="PUT", data=body
        )
    else:
        result = api_request(
            token, f"{API_BASE}/zones/{zone_id}/dns_records", method="POST", data=body
        )
    ok = bool(result and result.get("success"))
    verb = "update" if record_id else "create"
    if ok:
        print(f"{record_name} -> {current_ip}: {verb} ok")
    else:
        _error(f"{record_name} -> {current_ip}: {verb} FAILED{_api_errors(result)}")
    return ok


def main() -> int:
    token = os.environ.get("CF_API_TOKEN")
    if not token:
        _error("CF_API_TOKEN not set")
        return 1

    if not zone_is_valid(ZONE):
        _error(
            f"DDNS_ZONE is empty or malformed ({ZONE!r}) — the cluster-config "
            "postBuild substitution did not run. Refusing to touch DNS."
        )
        return 1

    for problem in RECORD_ERRORS:
        _error(f"DDNS_RECORDS entry {problem}")
    if RECORD_ERRORS or not records_are_valid(RECORDS):
        return 1

    current_ip = get_public_ip()
    if not current_ip:
        return 1
    print(f"Current public IP: {current_ip}")

    zone_id = get_zone_id(token)
    if not zone_id:
        return 1
    print(f"Zone ID: {zone_id}")

    failed = sum(
        not update_record(token, zone_id, record_name, current_ip, proxied)
        for record_name, proxied in RECORDS
    )
    if failed:
        _error(f"DDNS update FAILED for {failed} record(s)")
        return 1
    print("\nDDNS update complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
