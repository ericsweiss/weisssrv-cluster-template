#!/usr/bin/env python3
"""Shared half of a pod-reaper CronJob program: client, paging, ages, deletes.

A reaper supplies its selectors, guards and sweep loop, and catches OSError on
its list paths. Stdlib only. Contract: weisssrv-lib docs/SCRIPTS.md.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Iterator, NamedTuple
from urllib.parse import urlencode

API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# delete_with_logging results; only FAILED should fail the Job.
DELETED, GONE, FAILED = "deleted", "gone", "failed"


class KubeApi:
    """kube-apiserver client on the pod's ServiceAccount credentials."""

    def __init__(self, token: str, ca_file: str, timeout: int) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}
        self._ctx = ssl.create_default_context(cafile=ca_file)
        self._timeout = timeout

    @classmethod
    def from_service_account(cls, timeout: int, sa_dir: str = SA_DIR) -> KubeApi:
        with open(f"{sa_dir}/token") as fh:
            token = fh.read().strip()
        return cls(token, f"{sa_dir}/ca.crt", timeout)

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self._headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            API + path, method=method, headers=headers, data=data
        )
        with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as r:
            resp = r.read().decode()
        return json.loads(resp) if resp else {}


def paged(api, path: str, params: dict, page: int) -> Iterator[dict]:
    """Yield each list response, following the continue token.

    Paged so a large backlog cannot exhaust the container's memory limit.
    """
    params = dict(params, limit=str(page))
    while True:
        resp = api.request("GET", f"{path}?{urlencode(params)}")
        yield resp
        cont = resp.get("metadata", {}).get("continue")
        if not cont:
            return
        params["continue"] = cont


def list_items(api, path: str, params: dict, page: int) -> Iterator[dict]:
    """Yield every object of a paged list."""
    for resp in paged(api, path, params, page):
        yield from resp.get("items", [])


def parse_ts(ts: str) -> datetime:
    # The 'Z' is normalised so pre-3.11 interpreters parse it too.
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def creation_age_minutes(obj: dict, now: datetime) -> int | None:
    """Age from creationTimestamp; unparseable or absent -> None (KEEP)."""
    ts = obj.get("metadata", {}).get("creationTimestamp")
    if not ts:
        return None
    try:
        return int((now - parse_ts(ts)).total_seconds() // 60)
    except (TypeError, ValueError):
        return None


def terminal_age_minutes(pod: dict, now: datetime) -> int | None:
    """Age from the newest finishedAt; None keeps the pod when none finished or a
    started container lacks a parseable one. No startTime or creationTimestamp
    fallback: both predate completion and would delete a just-finished job."""
    status = pod.get("status", {})
    statuses = (status.get("initContainerStatuses", [])
                + status.get("containerStatuses", [])
                + status.get("ephemeralContainerStatuses", []))
    finished = []
    for cs in statuses:
        term = (cs.get("state", {}) or {}).get("terminated") or {}
        finished_at = term.get("finishedAt")
        if not finished_at:
            if cs.get("containerID"):
                return None
            continue
        try:
            finished.append(parse_ts(finished_at))
        except (TypeError, ValueError):
            return None
    if not finished:
        return None
    return int((now - max(finished)).total_seconds() // 60)


class Outcome(NamedTuple):
    """One sweep's result; a budget stop is partial progress, not a fault."""
    deleted: int
    had_errors: bool
    budget_hit: bool


def delete_pod(api, ns: str, name: str, uid: str) -> None:
    """Force-delete a terminal pod: grace 0 skips shutdown, so terminal pods only.

    The uid precondition makes the API reject (409) the delete when this name
    now refers to a different, newer pod after a stale list.
    """
    api.request("DELETE", f"/api/v1/namespaces/{ns}/pods/{name}",
                {"apiVersion": "v1", "kind": "DeleteOptions",
                 "gracePeriodSeconds": 0,
                 "preconditions": {"uid": uid}})


def delete_secret(api, ns: str, name: str, uid: str) -> None:
    api.request("DELETE", f"/api/v1/namespaces/{ns}/secrets/{name}",
                {"apiVersion": "v1", "kind": "DeleteOptions",
                 "preconditions": {"uid": uid}})


def is_replicaset_owned(pod: dict) -> bool:
    return any(
        owner.get("kind") == "ReplicaSet"
        for owner in pod.get("metadata", {}).get("ownerReferences") or []
    )


def live_pod_refs(api, ns: str, page: int, over_budget):
    """Return (live pod UIDs, imagePullSecrets names), or None.

    CRITICAL: None means the budget ran out with pages unread, so the set is
    incomplete and must never feed deletions, or a sweep takes live credentials.
    """
    uids: set[str] = set()
    pull_secrets: set[str] = set()
    for resp in paged(api, f"/api/v1/namespaces/{ns}/pods", {}, page):
        for pod in resp.get("items", []):
            uid = pod.get("metadata", {}).get("uid")
            if uid:
                uids.add(uid)
            for ref in pod.get("spec", {}).get("imagePullSecrets") or []:
                if ref.get("name"):
                    pull_secrets.add(ref["name"])
        # Returning here leaves the generator suspended, so the next page is
        # never requested.
        if resp.get("metadata", {}).get("continue") and over_budget():
            return None
    return uids, pull_secrets


def require_positive(values) -> None:
    """Fail closed on nonsense knobs: a negative age makes every terminal object
    eligible immediately, a zero page lifts the list limit, and a zero period
    cannot rotate."""
    invalid = {name: value for name, value in values if value <= 0}
    if invalid:
        details = ", ".join(f"{k}={v}" for k, v in sorted(invalid.items()))
        raise SystemExit(
            f"invalid reaper configuration; values must be positive: {details}"
        )


def delete_with_logging(delete, api, ns: str, name: str, uid: str, note: str,
                        log=print, kind: str = "") -> str:
    """-> DELETED, GONE or FAILED. A 404 or a 409 lost uid precondition is GONE;
    any other HTTP error or transport OSError, such as a read timeout, is FAILED.
    `kind` prefixes the object in each log line."""
    prefix = f"{kind} " if kind else ""
    obj = f"{prefix}{ns}/{name}"
    try:
        delete(api, ns, name, uid)
        log(f"DELETE {obj} ({note})", flush=True)
        return DELETED
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 409):
            log(f"GONE   {obj} (already deleted or replaced)", flush=True)
            return GONE
        log(f"ERROR delete {obj}: HTTP {exc.code}", flush=True)
        return FAILED
    except OSError as exc:
        log(f"ERROR delete {obj}: {exc!r}", flush=True)
        return FAILED


def rotate(items, now: datetime, period_seconds: int):
    """Start each scheduled run at a different offset, so a budget stop does not
    always strand the same tail. Derived from the clock with no persisted state,
    so period_seconds must equal the CronJob's schedule period."""
    if not items:
        return items
    offset = int(now.timestamp() // period_seconds) % len(items)
    return items[offset:] + items[:offset]


def run_reaper(load_config, run, api_factory=KubeApi.from_service_account) -> int:
    """The entry point every reaper shares: load config, build the client, start
    the budget clock, hand `run` a now and an over-budget predicate."""
    cfg = load_config()
    api = api_factory(cfg.api_timeout_seconds)
    start = time.monotonic()
    return run(
        api,
        cfg,
        datetime.now(timezone.utc),
        lambda: time.monotonic() - start > cfg.budget_seconds,
    )
