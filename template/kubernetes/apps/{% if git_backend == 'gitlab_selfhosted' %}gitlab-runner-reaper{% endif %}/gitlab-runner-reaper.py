#!/usr/bin/env python3
"""Delete leaked GitLab Runner executor pods and their per-job dockercfg Secrets.

Mounted into the gitlab-runner-reaper CronJob from the configMapGenerator in
this directory. Stdlib only; the guard model is in README.md.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import urlencode

API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class Config(NamedTuple):
    namespaces: list[str]
    # Guard 1: the pod label every executor carries and no manager pod does.
    # Comes from the CronJob env so this file holds no cluster identity.
    runner_class_label: str
    max_age_minutes: int
    # Leaked dockercfg Secrets have no termination time, so their clock runs from
    # creationTimestamp; a much longer floor than pods, since no job runs that
    # long -> any in-flight job's Secret is structurally protected.
    max_secret_age_minutes: int
    # Soft cap < the Job's activeDeadlineSeconds: stop cleanly and let the next
    # run continue, instead of a hard deadline-kill -> failed Job.
    budget_seconds: int
    # Short API timeout so a urlopen started near the budget cannot overrun the
    # hard deadline and defeat the soft stop.
    api_timeout_seconds: int
    # Runner pod specs/statuses are large, so a small page keeps a full page of
    # Pod objects well under the container's memory limit.
    page: int
    # One rotation slot per scheduled run - must equal the CronJob's schedule
    # period, so it comes from the same manifest (rotate_namespaces).
    rotate_period_seconds: int


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    namespaces = env.get("NAMESPACES", "").split()
    # Fail closed: with no namespaces the run sweeps nothing and exits 0, which
    # is indistinguishable from a clean sweep in the Job history.
    if not namespaces:
        raise SystemExit("NAMESPACES is empty - nothing to reap; refusing to run")
    label = env.get("RUNNER_CLASS_LABEL", "").strip()
    # Without the label selector every pod in the namespace is a candidate, and
    # only the name guard would stand between the sweep and a manager pod.
    if not label:
        raise SystemExit("RUNNER_CLASS_LABEL is empty; refusing to run")
    cfg = Config(
        namespaces=namespaces,
        runner_class_label=label,
        max_age_minutes=int(env.get("MAX_AGE_MINUTES", "30")),
        max_secret_age_minutes=int(env.get("MAX_SECRET_AGE_MINUTES", "180")),
        budget_seconds=int(env.get("BUDGET_SECONDS", "90")),
        api_timeout_seconds=int(env.get("API_TIMEOUT_SECONDS", "10")),
        page=int(env.get("PAGE_LIMIT", "25")),
        rotate_period_seconds=int(env.get("ROTATE_PERIOD_SECONDS", "900")),
    )
    # Fail closed on nonsense knobs: a negative age makes every terminal pod
    # eligible immediately; a zero page or rotation period disables bounds.
    invalid = {
        name: value
        for name, value in (
            ("MAX_AGE_MINUTES", cfg.max_age_minutes),
            ("MAX_SECRET_AGE_MINUTES", cfg.max_secret_age_minutes),
            ("BUDGET_SECONDS", cfg.budget_seconds),
            ("API_TIMEOUT_SECONDS", cfg.api_timeout_seconds),
            ("PAGE_LIMIT", cfg.page),
            ("ROTATE_PERIOD_SECONDS", cfg.rotate_period_seconds),
        )
        if value <= 0
    }
    if invalid:
        details = ", ".join(f"{k}={v}" for k, v in sorted(invalid.items()))
        raise SystemExit(f"invalid reaper configuration; values must be positive: {details}")
    return cfg


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
        req = urllib.request.Request(API + path, method=method, headers=headers, data=data)
        with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as r:
            resp = r.read().decode()
        return json.loads(resp) if resp else {}


def _paged(api, path: str, params: dict, page: int):
    """Yield each list response, following the continue token.

    Paging keeps a large leaked backlog from exhausting the container's memory
    limit before anything is deleted.
    """
    params = dict(params, limit=str(page))
    while True:
        resp = api.request("GET", f"{path}?{urlencode(params)}")
        yield resp
        cont = resp.get("metadata", {}).get("continue")
        if not cont:
            return
        params["continue"] = cont


def iter_terminal_pods(api, ns: str, phase: str, page: int, label: str):
    params = {"labelSelector": label, "fieldSelector": f"status.phase={phase}"}
    for resp in _paged(api, f"/api/v1/namespaces/{ns}/pods", params, page):
        yield from resp.get("items", [])


def iter_dockercfg_secrets(api, ns: str, page: int):
    # Server-side type filter, so the ServiceAccount never lists the runner
    # token or SA-token secrets.
    params = {"fieldSelector": "type=kubernetes.io/dockercfg"}
    for resp in _paged(api, f"/api/v1/namespaces/{ns}/secrets", params, page):
        yield from resp.get("items", [])


def live_pod_refs(api, ns: str, page: int, over_budget) -> tuple[set, set] | None:
    """Return (live pod UIDs, imagePullSecrets names), or None if incomplete.

    None means the budget ran out with pages unread. A partial set can make a
    running job's Secret look unreferenced, so it never feeds deletions.
    """
    uids: set[str] = set()
    pull_secrets: set[str] = set()
    for resp in _paged(api, f"/api/v1/namespaces/{ns}/pods", {}, page):
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


def parse_ts(ts: str) -> datetime:
    # Normalize the RFC3339 'Z' so this also parses on pre-3.11 interpreters.
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def terminal_age_minutes(pod: dict, now: datetime) -> int | None:
    """Age from the newest terminated finishedAt across all container statuses.

    A started container with no parseable finishedAt keeps the pod; one that
    never started is skipped. There is no startTime fallback. See README.md.
    """
    status = pod.get("status", {})
    statuses = (status.get("initContainerStatuses", [])
                + status.get("containerStatuses", [])
                + status.get("ephemeralContainerStatuses", []))
    finished = []
    for cs in statuses:
        term = (cs.get("state", {}) or {}).get("terminated") or {}
        fa = term.get("finishedAt")
        if not fa:
            if cs.get("containerID"):
                return None
            continue
        try:
            finished.append(parse_ts(fa))
        except (TypeError, ValueError):
            return None
    if not finished:
        return None
    return int((now - max(finished)).total_seconds() // 60)


def creation_age_minutes(obj: dict, now: datetime) -> int | None:
    """Age from creationTimestamp; unparseable/absent -> None (KEEP)."""
    ts = obj.get("metadata", {}).get("creationTimestamp")
    if not ts:
        return None
    try:
        return int((now - parse_ts(ts)).total_seconds() // 60)
    except (TypeError, ValueError):
        return None


def is_executor_name(name: str) -> bool:
    # Guard 3: executor pods are runner-<id>-project-<n>-concurrent-<n>-<hash>;
    # the manager is gitlab-runner[-privileged]-<hash>.
    return (name.startswith("runner-")
            and "-project-" in name
            and "-concurrent-" in name)


def is_runner_secret_name(name: str) -> bool:
    # Guard 2: the executor's Secret carries the same ProjectUniqueName markers
    # as its pod. The `runner-` prefix alone would also match a hand-created
    # dockercfg Secret, so require the full executor shape.
    return is_executor_name(name)


def secret_referenced(secret: dict, live_uids: set, pull_secrets: set) -> bool:
    # Guard 3: still owned or used by a pod -> keep. Its job's pod has simply
    # not been reaped yet.
    meta = secret.get("metadata", {})
    if meta.get("name") in pull_secrets:
        return True
    for owner in meta.get("ownerReferences") or []:
        if owner.get("uid") in live_uids:
            return True
    return False


def delete_pod(api, ns: str, name: str, uid: str) -> None:
    # uid precondition: the API rejects (409) the delete if this name now refers
    # to a different, newer pod after a stale list.
    api.request("DELETE", f"/api/v1/namespaces/{ns}/pods/{name}",
                {"apiVersion": "v1", "kind": "DeleteOptions",
                 "gracePeriodSeconds": 0,
                 "preconditions": {"uid": uid}})


def delete_secret(api, ns: str, name: str, uid: str) -> None:
    api.request("DELETE", f"/api/v1/namespaces/{ns}/secrets/{name}",
                {"apiVersion": "v1", "kind": "DeleteOptions",
                 "preconditions": {"uid": uid}})


class Outcome(NamedTuple):
    deleted: int
    had_errors: bool
    budget_hit: bool


def reap_pods(api, cfg: Config, ns: str, now: datetime, over_budget, log=print) -> Outcome:
    deleted = 0
    had_errors = False
    # The field selector is AND-only and cannot OR phases, so query each
    # terminal phase separately.
    for phase in ("Succeeded", "Failed"):
        try:
            for pod in iter_terminal_pods(api, ns, phase, cfg.page, cfg.runner_class_label):
                if over_budget():
                    log(f"BUDGET {cfg.budget_seconds}s reached; "
                        "leaving remaining pods for the next run", flush=True)
                    return Outcome(deleted, had_errors, True)
                name = pod["metadata"]["name"]
                if not is_executor_name(name):
                    log(f"SKIP   {ns}/{name} (name guard)", flush=True)
                    continue
                age = terminal_age_minutes(pod, now)
                if age is None:
                    log(f"KEEP   {ns}/{name} (phase={phase} age=unknown)", flush=True)
                    continue
                if age < cfg.max_age_minutes:
                    log(f"KEEP   {ns}/{name} (phase={phase} age={age}m < {cfg.max_age_minutes}m)", flush=True)
                    continue
                try:
                    delete_pod(api, ns, name, pod["metadata"]["uid"])
                    log(f"DELETE {ns}/{name} (phase={phase} age={age}m)", flush=True)
                    deleted += 1
                except urllib.error.HTTPError as e:
                    # 404 = already gone (the manager won the race); 409 = uid
                    # precondition lost. Both harmless no-ops.
                    if e.code in (404, 409):
                        log(f"GONE   {ns}/{name} (already deleted or replaced)", flush=True)
                    else:
                        log(f"ERROR delete {ns}/{name}: HTTP {e.code}", flush=True)
                        had_errors = True
                except urllib.error.URLError as e:
                    log(f"ERROR delete {ns}/{name}: {e}", flush=True)
                    had_errors = True
        except urllib.error.URLError as e:
            # A list/paging failure (broken RBAC, API outage) must FAIL the Job,
            # not silently no-op past backoffLimit.
            log(f"ERROR list {ns}/{phase}: {e}", flush=True)
            had_errors = True
    return Outcome(deleted, had_errors, False)


def reap_secrets(api, cfg: Config, ns: str, now: datetime, over_budget, log=print) -> Outcome:
    deleted = 0
    had_errors = False
    try:
        refs = live_pod_refs(api, ns, cfg.page, over_budget)
        if refs is None:
            # Hard stop rather than a partial sweep: without a complete
            # live-ref set, a secret judged unreferenced may belong to a
            # running job.
            log(f"BUDGET {cfg.budget_seconds}s reached while listing live pods in {ns}; "
                "skipping the secret sweep (an incomplete live-pod set could not "
                "prove a secret unreferenced)", flush=True)
            return Outcome(deleted, had_errors, True)
        live_uids, pull_secrets = refs
        for secret in iter_dockercfg_secrets(api, ns, cfg.page):
            if over_budget():
                log(f"BUDGET {cfg.budget_seconds}s reached; "
                    "leaving remaining secrets for the next run", flush=True)
                return Outcome(deleted, had_errors, True)
            name = secret["metadata"]["name"]
            if not is_runner_secret_name(name):
                log(f"SKIP   secret {ns}/{name} (name guard)", flush=True)
                continue
            if secret_referenced(secret, live_uids, pull_secrets):
                log(f"KEEP   secret {ns}/{name} (referenced by live pod)", flush=True)
                continue
            age = creation_age_minutes(secret, now)
            if age is None:
                log(f"KEEP   secret {ns}/{name} (age=unknown)", flush=True)
                continue
            if age < cfg.max_secret_age_minutes:
                log(f"KEEP   secret {ns}/{name} (age={age}m < {cfg.max_secret_age_minutes}m)", flush=True)
                continue
            try:
                delete_secret(api, ns, name, secret["metadata"]["uid"])
                log(f"DELETE secret {ns}/{name} (age={age}m)", flush=True)
                deleted += 1
            except urllib.error.HTTPError as e:
                if e.code in (404, 409):
                    log(f"GONE   secret {ns}/{name} (already deleted or replaced)", flush=True)
                else:
                    log(f"ERROR delete secret {ns}/{name}: HTTP {e.code}", flush=True)
                    had_errors = True
            except urllib.error.URLError as e:
                log(f"ERROR delete secret {ns}/{name}: {e}", flush=True)
                had_errors = True
    except urllib.error.URLError as e:
        log(f"ERROR list secrets {ns}: {e}", flush=True)
        had_errors = True
    return Outcome(deleted, had_errors, False)


def rotate_namespaces(namespaces: list[str], now: datetime,
                      period_seconds: int = 900) -> list[str]:
    """Namespaces rotated one slot per scheduled run, so no tail is starved.

    The offset keys on the wall-clock slot and keeps no state, so
    period_seconds must equal the CronJob's schedule period.
    """
    if not namespaces:
        return namespaces
    offset = int(now.timestamp() // period_seconds) % len(namespaces)
    return namespaces[offset:] + namespaces[:offset]


def run(api, cfg: Config, now: datetime, over_budget, log=print) -> int:
    """Reap every namespace; -> process exit code."""
    deleted = deleted_secrets = 0
    had_errors = False
    unvisited: list[str] = []
    order = rotate_namespaces(cfg.namespaces, now, cfg.rotate_period_seconds)
    for index, ns in enumerate(order):
        pods = reap_pods(api, cfg, ns, now, over_budget, log)
        deleted += pods.deleted
        had_errors = had_errors or pods.had_errors
        if pods.budget_hit:
            unvisited = order[index:]
            break
        # Pods are reaped before secrets, so a leaked pair clears over
        # consecutive runs (guard 3 keeps a Secret while its pod still exists).
        secrets = reap_secrets(api, cfg, ns, now, over_budget, log)
        deleted_secrets += secrets.deleted
        had_errors = had_errors or secrets.had_errors
        if secrets.budget_hit:
            unvisited = order[index:]
            break
    # A budget stop exits 0; this line is the only signal the run was partial,
    # so alert on it if you add reaper rules.
    if unvisited:
        log(f"BUDGET STOP after {cfg.budget_seconds}s; "
            f"not fully reaped this run: {' '.join(unvisited)}", flush=True)
    log(f"reaper done: deleted={deleted} secrets={deleted_secrets} "
        f"budget_stop={'yes' if unvisited else 'no'}", flush=True)
    # Non-zero so the Job fails, backoffLimit retries, and a persistent failure
    # surfaces (failed Job history + KubeJobFailed) instead of a silent no-op. A
    # clean budget stop exits 0 — that is partial progress, not a fault.
    return 1 if had_errors else 0


def main() -> int:
    cfg = load_config()
    api = KubeApi.from_service_account(cfg.api_timeout_seconds)
    start = time.monotonic()
    return run(
        api,
        cfg,
        datetime.now(timezone.utc),
        lambda: time.monotonic() - start > cfg.budget_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
