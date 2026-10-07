#!/usr/bin/env python3
"""Point the external zone's A records at the site's current public IPv4.

Run by the CronJob beside this file. Configuration is environment-only and the
variables, along with the record-ownership split, are described in README.md.
"""

import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("CF_API_TOKEN")
ZONE = os.environ.get("DDNS_ZONE", "").strip()
RECORDS = os.environ.get("DDNS_RECORDS", "").strip()

API = "https://api.cloudflare.com/client/v4"


def api(url, method="GET", data=None):
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode(errors="replace")
        except Exception:
            detail = "<unreadable>"
        print("ERROR: HTTP %s on %s %s: %s" % (e.code, method, url, detail), file=sys.stderr)
    except Exception as e:
        print("ERROR: %s on %s %s: %s" % (type(e).__name__, method, url, e), file=sys.stderr)
    return None


DEFAULT_IP_PROVIDERS = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
)


def public_ip(providers=DEFAULT_IP_PROVIDERS, attempts=3, sleep=time.sleep):
    """Return the current public IPv4, accepting only a global address.

    Providers are tried in turn so one being down does not fail the job.
    """
    for attempt in range(attempts):
        for url in providers:
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    candidate = resp.read().decode().strip()
                if ipaddress.IPv4Address(candidate).is_global:
                    return candidate
                print("WARN: %s returned %r, trying next" % (url, candidate), file=sys.stderr)
            except Exception as e:
                print("WARN: %s failed: %s" % (url, e), file=sys.stderr)
        if attempt < attempts - 1:
            sleep(5)
    return None


def update(zone_id, name, current, proxied_on_create):
    """proxied and ttl seed CREATION only. On update the record's existing
    values are preserved, so this job and Terraform cannot flip-flop them on
    every cycle: Terraform owns proxied/ttl, this job owns the address."""
    data = api(
        "%s/zones/%s/dns_records?type=A&name=%s"
        % (API, zone_id, urllib.parse.quote(name, safe=""))
    )
    if not data or not data.get("success"):
        # stderr, like every other failure here: the CronJob's stdout is the
        # record-by-record report an operator skims, and a failure buried in it
        # reads as a successful run.
        print("ERROR: cannot query %s" % name, file=sys.stderr)
        return False
    records = data.get("result") or []
    if len(records) > 1:
        # Updating just the first would leave stale siblings answering
        # intermittently; single-record ownership is this job's contract.
        print("ERROR: multiple A records for %s; refusing a partial update" % name,
              file=sys.stderr)
        return False
    existing = records[0] if records else None
    if existing and existing.get("content") == current:
        print("%s unchanged" % name)
        return True
    payload = {
        "type": "A",
        "name": name,
        "content": current,
        "ttl": existing.get("ttl", 1) if existing else 1,
        "proxied": existing.get("proxied", proxied_on_create) if existing else proxied_on_create,
    }
    # PUT replaces the whole record: carry every Terraform-owned decoration
    # forward or the address update silently erases it.
    if existing:
        for key in ("comment", "tags", "settings"):
            if existing.get(key) is not None:
                payload[key] = existing[key]
    if existing:
        result = api(
            "%s/zones/%s/dns_records/%s" % (API, zone_id, existing["id"]),
            method="PUT",
            data=payload,
        )
    else:
        result = api("%s/zones/%s/dns_records" % (API, zone_id), method="POST", data=payload)
    ok = bool(result and result.get("success"))
    # Failures go to stderr: the CronJob's log is scraped, and a record that did
    # not update must be findable without parsing the success line's wording.
    print(
        "%s -> %s: %s" % (name, current, "ok" if ok else "FAILED"),
        file=sys.stdout if ok else sys.stderr,
    )
    return ok


def main():
    if not TOKEN or not ZONE:
        sys.exit("ERROR: CF_API_TOKEN and DDNS_ZONE are required")

    # Fail closed: an empty list would exit 0 managing nothing, and a blank
    # name (e.g. a stray ':false') would issue an unbounded record query.
    entries = [r.strip() for r in RECORDS.split(",") if r.strip()]
    if not entries:
        sys.exit("ERROR: DDNS_RECORDS must name at least one record")
    for entry in entries:
        # Check shape first: `partition` swallows extra colons into the flag,
        # so `a.zone:false:oops` would pass as a record published through the
        # proxy rather than direct.
        if entry.count(":") > 1:
            sys.exit(
                "ERROR: DDNS_RECORDS entry %r is not a name[:proxied] pair" % entry
            )
        name, _, flag = entry.partition(":")
        if not name.strip():
            sys.exit("ERROR: DDNS_RECORDS entry %r has no record name" % entry)
        # Only `false` turns the proxy off, so reject every other spelling:
        # `:no`, `:0`, a bare trailing colon would leave the record proxied
        # when a direct one was asked for.
        if ":" in entry and flag.strip().lower() not in ("true", "false"):
            sys.exit(
                "ERROR: DDNS_RECORDS entry %r has a non-boolean proxied flag "
                "(want `true` or `false`)" % entry
            )

    current = public_ip()
    if not current:
        sys.exit("ERROR: no usable public IPv4 after retries")
    print("public IPv4: %s" % current)

    zones = api("%s/zones?name=%s" % (API, urllib.parse.quote(ZONE, safe="")))
    if not zones or not zones.get("success") or not zones.get("result"):
        sys.exit("ERROR: zone %s not found" % ZONE)
    zone_id = zones["result"][0]["id"]

    ok = True
    for entry in entries:
        name, _, flag = entry.partition(":")
        ok = update(zone_id, name.strip(), current, flag.strip().lower() != "false") and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
