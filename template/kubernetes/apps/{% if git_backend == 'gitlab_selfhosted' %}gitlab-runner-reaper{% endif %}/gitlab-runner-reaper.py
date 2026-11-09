#!/usr/bin/env python3
"""Delete leaked GitLab Runner executor pods and their per-job dockercfg Secrets.

Mounted with the vendored kube_reaper.py by the configMapGenerator here; stdlib
only. What leaks and the guards: README.md here.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import NamedTuple

import kube_reaper
from kube_reaper import FAILED, Outcome, delete_pod, delete_secret, delete_with_logging


class Config(NamedTuple):
    namespaces: list[str]
    # Pod guard 1: the label every executor carries and no manager pod does.
    # Comes from the CronJob env so this file holds no cluster identity.
    runner_class_label: str
    max_age_minutes: int
    # Leaked dockercfg Secrets have no termination time, so their clock runs from
    # creationTimestamp, with a floor longer than any job runs.
    max_secret_age_minutes: int
    # Soft cap under the Job's activeDeadlineSeconds: stop cleanly, finish next run.
    budget_seconds: int
    # Short, so a request started near the budget cannot overrun the deadline.
    api_timeout_seconds: int
    # Runner pod objects are large, so a small page stays under the memory limit.
    page: int
    # One rotation slot per scheduled run: equals the CronJob's schedule period.
    rotate_period_seconds: int


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    namespaces = env.get("NAMESPACES", "").split()
    # Fail closed: with no namespaces the run sweeps nothing and exits 0, which
    # is indistinguishable from a clean sweep in the Job history.
    if not namespaces:
        raise SystemExit("NAMESPACES is empty — nothing to reap; refusing to run")
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
    kube_reaper.require_positive((
        ("MAX_AGE_MINUTES", cfg.max_age_minutes),
        ("MAX_SECRET_AGE_MINUTES", cfg.max_secret_age_minutes),
        ("BUDGET_SECONDS", cfg.budget_seconds),
        ("API_TIMEOUT_SECONDS", cfg.api_timeout_seconds),
        ("PAGE_LIMIT", cfg.page),
        ("ROTATE_PERIOD_SECONDS", cfg.rotate_period_seconds),
    ))
    return cfg


def iter_terminal_pods(api, ns: str, phase: str, page: int, label: str):
    params = {"labelSelector": label, "fieldSelector": f"status.phase={phase}"}
    return kube_reaper.list_items(api, f"/api/v1/namespaces/{ns}/pods", params, page)


def iter_dockercfg_secrets(api, ns: str, page: int):
    # Server-side type filter, so the ServiceAccount never lists the runner
    # token or SA-token secrets.
    params = {"fieldSelector": "type=kubernetes.io/dockercfg"}
    return kube_reaper.list_items(api, f"/api/v1/namespaces/{ns}/secrets", params, page)


def is_executor_name(name: str) -> bool:
    # Pod guard 3: executors are runner-<id>-project-<n>-concurrent-<n>-<hash>;
    # the manager is gitlab-runner[-privileged]-<hash>.
    return (name.startswith("runner-")
            and "-project-" in name
            and "-concurrent-" in name)


def is_runner_secret_name(name: str) -> bool:
    # Secret guard 2: the executor's credentials Secret carries the same name
    # markers as its pod. The prefix alone would match a hand-created Secret.
    return is_executor_name(name)


def secret_referenced(secret: dict, live_uids: set, pull_secrets: set) -> bool:
    # Secret guard 3: still owned or used by a pod that is not reaped yet.
    meta = secret.get("metadata", {})
    if meta.get("name") in pull_secrets:
        return True
    return any(
        owner.get("uid") in live_uids for owner in meta.get("ownerReferences") or []
    )


def reap_pods(api, cfg: Config, ns: str, now: datetime, over_budget, log=print) -> Outcome:
    deleted = 0
    had_errors = False
    # The field selector is AND-only and cannot OR phases, so each terminal
    # phase is its own query.
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
                age = kube_reaper.terminal_age_minutes(pod, now)
                if age is None:
                    log(f"KEEP   {ns}/{name} (phase={phase} age=unknown)", flush=True)
                    continue
                if age < cfg.max_age_minutes:
                    log(f"KEEP   {ns}/{name} (phase={phase} age={age}m "
                        f"< {cfg.max_age_minutes}m)", flush=True)
                    continue
                result = delete_with_logging(delete_pod, api, ns, name,
                                             pod["metadata"]["uid"],
                                             f"phase={phase} age={age}m", log)
                deleted += result == kube_reaper.DELETED
                had_errors = had_errors or result == FAILED
        except OSError as e:
            # A list or paging failure must FAIL the Job, not no-op past backoffLimit.
            log(f"ERROR list {ns}/{phase}: {e}", flush=True)
            had_errors = True
    return Outcome(deleted, had_errors, False)


def reap_secrets(api, cfg: Config, ns: str, now: datetime, over_budget, log=print) -> Outcome:
    deleted = 0
    had_errors = False
    try:
        refs = kube_reaper.live_pod_refs(api, ns, cfg.page, over_budget)
        if refs is None:
            # An incomplete live-pod set cannot prove secret guard 3, so the
            # namespace's secret sweep stops here.
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
            age = kube_reaper.creation_age_minutes(secret, now)
            if age is None:
                log(f"KEEP   secret {ns}/{name} (age=unknown)", flush=True)
                continue
            if age < cfg.max_secret_age_minutes:
                log(f"KEEP   secret {ns}/{name} (age={age}m "
                    f"< {cfg.max_secret_age_minutes}m)", flush=True)
                continue
            result = delete_with_logging(delete_secret, api, ns, name,
                                         secret["metadata"]["uid"], f"age={age}m",
                                         log, kind="secret")
            deleted += result == kube_reaper.DELETED
            had_errors = had_errors or result == FAILED
    except OSError as e:
        log(f"ERROR list secrets {ns}: {e}", flush=True)
        had_errors = True
    return Outcome(deleted, had_errors, False)


def run(api, cfg: Config, now: datetime, over_budget, log=print) -> int:
    """Reap every namespace; -> process exit code."""
    deleted = deleted_secrets = 0
    had_errors = False
    unvisited: list[str] = []
    order = kube_reaper.rotate(cfg.namespaces, now, cfg.rotate_period_seconds)
    for index, ns in enumerate(order):
        pods = reap_pods(api, cfg, ns, now, over_budget, log)
        deleted += pods.deleted
        had_errors = had_errors or pods.had_errors
        if pods.budget_hit:
            unvisited = order[index:]
            break
        # Pods go before secrets, so a leaked pair clears over consecutive runs.
        secrets = reap_secrets(api, cfg, ns, now, over_budget, log)
        deleted_secrets += secrets.deleted
        had_errors = had_errors or secrets.had_errors
        if secrets.budget_hit:
            unvisited = order[index:]
            break
    # A budget stop exits 0; this line is the only sign the run was partial, and
    # GitlabRunnerReaperPartialSweep alerts on it.
    if unvisited:
        log(f"BUDGET STOP after {cfg.budget_seconds}s; "
            f"not fully reaped this run: {' '.join(unvisited)}", flush=True)
    log(f"reaper done: deleted={deleted} secrets={deleted_secrets} "
        f"budget_stop={'yes' if unvisited else 'no'}", flush=True)
    # Non-zero fails the Job, so backoffLimit retries and a persistent failure
    # surfaces as KubeJobFailed. A budget stop is partial progress, not a fault.
    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(kube_reaper.run_reaper(load_config, run))
