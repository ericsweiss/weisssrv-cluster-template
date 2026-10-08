"""Guards between the gitlab-runner-reaper CronJob and deleting a live CI job
pod or an in-flight job's registry credential Secret. The program is loaded by
path from kubernetes/apps/gitlab-runner-reaper/."""
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from conftest import REPO, load_script

MODULE_PATH = REPO / "kubernetes/apps/gitlab-runner-reaper/gitlab-runner-reaper.py"
CRONJOB_PATH = MODULE_PATH.parent / "cronjob.yaml"

pytestmark = pytest.mark.skipif(
    not MODULE_PATH.is_file(),
    reason="this cluster ships no gitlab-runner-reaper module (git_backend)",
)

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


def _load():
    return load_script(MODULE_PATH)


@pytest.fixture(scope="module")
def reaper():
    return _load()


def _ts(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cronjob() -> dict:
    """The manifest the program actually runs under — read, never restated."""
    return yaml.safe_load(CRONJOB_PATH.read_text())


def _job_spec() -> dict:
    return _cronjob()["spec"]["jobTemplate"]["spec"]


def _container_env() -> dict:
    container = _job_spec()["template"]["spec"]["containers"][0]
    return {var["name"]: var["value"] for var in container["env"]}


def _schedule_period_seconds(schedule: str) -> int:
    """Fire interval of a `*/N * * * *` schedule, in seconds."""
    minute, *rest = schedule.split()
    assert rest == ["*"] * 4, f"unhandled schedule {schedule!r}"
    assert minute.startswith("*/"), f"unhandled schedule {schedule!r}"
    return int(minute[2:]) * 60


def pod(name, phase="Succeeded", uid="pod-uid", *, container_ages=(90,), init=None,
        started_unfinished=False, bad_timestamp=False):
    """A terminal executor pod as the list API returns it."""
    statuses = []
    for age in container_ages:
        statuses.append({
            "name": "build",
            "containerID": "containerd://abc",
            "state": {"terminated": {"exitCode": 0, "finishedAt": _ts(age)}},
        })
    if started_unfinished:
        statuses.append({"name": "svc", "containerID": "containerd://def", "state": {"running": {}}})
    if bad_timestamp:
        statuses.append({
            "name": "helper",
            "containerID": "containerd://ghi",
            "state": {"terminated": {"finishedAt": "not-a-timestamp"}},
        })
    doc = {
        "metadata": {"name": name, "namespace": "gitlab-runner", "uid": uid,
                     "creationTimestamp": _ts(240)},
        "spec": {},
        "status": {"phase": phase, "startTime": _ts(240), "containerStatuses": statuses},
    }
    if init is not None:
        doc["status"]["initContainerStatuses"] = init
    return doc


def secret(name, uid="sec-uid", *, age=240, owner_uid=None):
    """A per-job dockercfg Secret as the list API returns it."""
    meta = {"name": name, "namespace": "gitlab-runner", "uid": uid,
            "creationTimestamp": _ts(age)}
    if owner_uid:
        meta["ownerReferences"] = [{"kind": "Pod", "name": "runner-x", "uid": owner_uid}]
    return {"metadata": meta, "type": "kubernetes.io/dockercfg"}


EXECUTOR = "runner-hz7ktcs-project-42-concurrent-0-8f3a1b2c"
MANAGER = "gitlab-runner-privileged-7d9c8f5b64-2xqzt"


def executor_secret(tag: str) -> str:
    """A per-job credentials Secret name carrying the -project-/-concurrent-
    markers the name guard requires."""
    return f"runner-hz7ktcs-project-42-concurrent-0-{tag}"


class FakeApi:
    """Records requests; replays canned list pages and delete outcomes."""

    def __init__(self, pages=None, deletes=None):
        # pages: list of (path_fragment, response) consumed in order per prefix.
        self.pages = pages or {}
        self.deletes = deletes or {}
        self.calls = []
        self.raw_paths = []

    def request(self, method, path, body=None):
        # calls keeps the bare path (what the delete assertions match on);
        # raw_paths keeps the query string, which is where the label/field
        # selectors that scope this ServiceAccount's reads actually live.
        self.calls.append((method, path.split("?")[0], body))
        self.raw_paths.append(path)
        if method == "DELETE":
            outcome = self.deletes.get(path.split("/")[-1])
            if isinstance(outcome, Exception):
                raise outcome
            return {}
        for prefix, responses in self.pages.items():
            if prefix in path:
                return responses.pop(0) if len(responses) > 1 else responses[0]
        return {"items": []}


def http_error(code):
    return urllib.error.HTTPError("http://k8s", code, "boom", None, None)


def never_over_budget():
    return False


def test_module_exists():
    assert MODULE_PATH.is_file(), f"{MODULE_PATH} missing — the CronJob mounts it"


def test_the_label_selector_comes_from_the_cronjob(reaper):
    # The module holds no cluster identity: the label arrives as env, and Flux
    # substitutes the placeholder in the manifest.
    assert _container_env()["RUNNER_CLASS_LABEL"] == \
        "${cluster_node_label_domain}/runner-class"
    assert reaper.load_config(_container_env()).runner_class_label == \
        _container_env()["RUNNER_CLASS_LABEL"]


def test_load_config_fails_closed_without_a_label(reaper):
    # With no label selector every pod in the namespace is a candidate and only
    # the name guard stands between the sweep and a manager pod.
    with pytest.raises(SystemExit, match="RUNNER_CLASS_LABEL"):
        reaper.load_config({"NAMESPACES": "gitlab-runner"})


# --- name guards -----------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    (EXECUTOR, True),
    (MANAGER, False),
    ("runner-hz7ktcs-project-42", False),          # no -concurrent-
    ("runner-hz7ktcs-concurrent-0-8f3a", False),   # no -project-
    ("gitlab-runner-token-abc", False),
])
def test_is_executor_name(reaper, name, expected):
    assert reaper.is_executor_name(name) is expected


@pytest.mark.parametrize("name,expected", [
    (EXECUTOR + "-dockercfg", True),
    (executor_secret("9c1e"), True),
    ("gitlab-runner-token", False),
    ("gitlab-runner-privileged-token-xyz", False),
    # The prefix alone is NOT the guard: a hand-created dockercfg Secret named
    # for a registry would otherwise become eligible and be reaped at 180m.
    ("runner-registry", False),
    ("runner-hz7ktcs-project-42", False),          # no -concurrent-
    ("runner-hz7ktcs-concurrent-0-8f3a", False),   # no -project-
])
def test_is_runner_secret_name(reaper, name, expected):
    assert reaper.is_runner_secret_name(name) is expected


# --- terminal_age_minutes: the four branches -------------------------------

def test_age_is_taken_from_the_newest_termination(reaper):
    p = pod(EXECUTOR, container_ages=(200, 45, 120))
    assert reaper.terminal_age_minutes(p, NOW) == 45


def test_age_spans_init_containers(reaper):
    # An init-container failure leaves finishedAt only in initContainerStatuses
    # and the regular containers never start.
    p = pod(EXECUTOR, phase="Failed", container_ages=())
    p["status"]["initContainerStatuses"] = [{
        "name": "prepare",
        "containerID": "containerd://init",
        "state": {"terminated": {"exitCode": 1, "finishedAt": _ts(75)}},
    }]
    p["status"]["containerStatuses"] = [{"name": "build", "state": {"waiting": {}}}]
    assert reaper.terminal_age_minutes(p, NOW) == 75


def test_started_but_unfinished_container_keeps_the_pod(reaper):
    assert reaper.terminal_age_minutes(pod(EXECUTOR, started_unfinished=True), NOW) is None


def test_unparseable_timestamp_keeps_the_pod(reaper):
    assert reaper.terminal_age_minutes(pod(EXECUTOR, bad_timestamp=True), NOW) is None


def test_no_terminated_container_keeps_the_pod(reaper):
    assert reaper.terminal_age_minutes(pod(EXECUTOR, container_ages=()), NOW) is None


def test_age_never_falls_back_to_start_time(reaper):
    # startTime is 240m old; only finishedAt (10m) may drive the decision, or a
    # freshly-terminal long job would be deleted mid-flight.
    assert reaper.terminal_age_minutes(pod(EXECUTOR, container_ages=(10,)), NOW) == 10


# --- secret guards ---------------------------------------------------------

def test_secret_referenced_by_image_pull_secret(reaper):
    s = secret("runner-a-dockercfg")
    assert reaper.secret_referenced(s, set(), {"runner-a-dockercfg"}) is True


def test_secret_referenced_by_live_owner(reaper):
    s = secret("runner-a-dockercfg", owner_uid="pod-1")
    assert reaper.secret_referenced(s, {"pod-1"}, set()) is True
    assert reaper.secret_referenced(s, {"pod-2"}, set()) is False


def test_creation_age_minutes(reaper):
    assert reaper.creation_age_minutes(secret("runner-a", age=300), NOW) == 300
    assert reaper.creation_age_minutes({"metadata": {}}, NOW) is None
    assert reaper.creation_age_minutes(
        {"metadata": {"creationTimestamp": "nope"}}, NOW) is None


# --- config ----------------------------------------------------------------

def test_load_config_defaults_match_the_manifest(reaper):
    env = _container_env()
    cfg = reaper.load_config({"NAMESPACES": env["NAMESPACES"],
                              "RUNNER_CLASS_LABEL": env["RUNNER_CLASS_LABEL"]})
    assert cfg.namespaces == env["NAMESPACES"].split()
    # Every knob the manifest sets explicitly must equal the code default, or
    # the two drift silently: the CronJob wins in-cluster while every test here
    # exercises the defaults.
    assert set(env) <= {name.upper() for name in cfg._asdict()}, \
        "the CronJob sets an env var load_config does not read"
    for name, value in cfg._asdict().items():
        manifest = env.get(name.upper())
        if manifest is not None and name not in ("namespaces", "runner_class_label"):
            assert value == int(manifest), f"{name} differs from the CronJob env"
    # activeDeadlineSeconds covers the whole Job INCLUDING retries, so the
    # per-attempt share is deadline / (backoffLimit + 1): the soft budget must
    # stay under that, with room for one API timeout to overrun it.
    job = _job_spec()
    per_attempt = job["activeDeadlineSeconds"] / (job["backoffLimit"] + 1)
    assert cfg.budget_seconds < per_attempt
    assert cfg.budget_seconds + cfg.api_timeout_seconds <= per_attempt


def test_load_config_fails_closed_on_an_empty_namespace_list(reaper):
    # An empty list sweeps nothing and exits 0 — indistinguishable from a clean
    # run in the Job history, so it must refuse to start instead.
    label = {"RUNNER_CLASS_LABEL": "example.test/runner-class"}
    for env in ({}, dict(label, NAMESPACES="   ")):
        with pytest.raises(SystemExit):
            reaper.load_config(env)


def test_load_config_rejects_non_positive_values(reaper):
    base = {"NAMESPACES": "a", "RUNNER_CLASS_LABEL": "example.test/runner-class"}
    with pytest.raises(SystemExit, match="MAX_AGE_MINUTES=-1"):
        reaper.load_config(dict(base, MAX_AGE_MINUTES="-1"))
    with pytest.raises(SystemExit, match="PAGE_LIMIT=0"):
        reaper.load_config(dict(base, PAGE_LIMIT="0"))


def test_rotation_period_equals_the_cronjob_schedule():
    # The rotation offset is a wall-clock slot index, so a period that is not
    # the fire interval re-runs one head or skips another.
    assert int(_container_env()["ROTATE_PERIOD_SECONDS"]) == _schedule_period_seconds(
        _cronjob()["spec"]["schedule"])


# --- pod reaping -----------------------------------------------------------

def _cfg(reaper, **over):
    base = {"NAMESPACES": "gitlab-runner", "RUNNER_CLASS_LABEL": "example.test/runner-class"}
    base.update(over)
    return reaper.load_config(base)


def test_reap_pods_deletes_only_old_executor_pods(reaper):
    api = FakeApi(pages={"/pods": [{"items": [
        pod(EXECUTOR, uid="u1", container_ages=(90,)),
        pod(EXECUTOR + "-b", uid="u2", container_ages=(5,)),
        pod(MANAGER, uid="u3", container_ages=(600,)),
    ]}]})
    out = reaper.reap_pods(api, _cfg(reaper), "gitlab-runner", NOW,
                           never_over_budget, log=lambda *a, **k: None)
    deleted = [p for m, p, _ in api.calls if m == "DELETE"]
    # Two terminal phases are queried, so the same page is served twice.
    assert set(deleted) == {"/api/v1/namespaces/gitlab-runner/pods/" + EXECUTOR}
    assert out.deleted == 2 and out.had_errors is False


def test_reap_pods_sends_a_uid_precondition(reaper):
    api = FakeApi(pages={"/pods": [{"items": [pod(EXECUTOR, uid="u1")]}]})
    reaper.reap_pods(api, _cfg(reaper), "gitlab-runner", NOW,
                     never_over_budget, log=lambda *a, **k: None)
    body = next(b for m, _p, b in api.calls if m == "DELETE")
    assert body["preconditions"] == {"uid": "u1"}
    assert body["gracePeriodSeconds"] == 0


@pytest.mark.parametrize("code,errors", [(404, False), (409, False), (500, True)])
def test_reap_pods_treats_races_as_no_ops_and_real_errors_as_failures(reaper, code, errors):
    api = FakeApi(pages={"/pods": [{"items": [pod(EXECUTOR, uid="u1")]}]},
                  deletes={EXECUTOR: http_error(code)})
    out = reaper.reap_pods(api, _cfg(reaper), "gitlab-runner", NOW,
                           never_over_budget, log=lambda *a, **k: None)
    assert out.had_errors is errors
    assert out.deleted == 0


def test_reap_pods_counts_a_transport_error_on_delete(reaper):
    # A connection reset mid-delete is not a race — it must fail the Job so the
    # backoff retries rather than the run reporting a clean sweep.
    api = FakeApi(pages={"/pods": [{"items": [pod(EXECUTOR, uid="u1")]}]},
                  deletes={EXECUTOR: urllib.error.URLError("connection reset")})
    out = reaper.reap_pods(api, _cfg(reaper), "gitlab-runner", NOW,
                           never_over_budget, log=lambda *a, **k: None)
    assert out.had_errors is True and out.deleted == 0


def test_reap_pods_keeps_a_pod_whose_age_is_unknown(reaper):
    # age=None means a container is still running: a LIVE CI job. No delete may
    # be issued for it, and the run stays clean.
    api = FakeApi(pages={"/pods": [{"items": [
        pod(EXECUTOR, uid="u1", container_ages=(90,), started_unfinished=True),
    ]}]})
    out = reaper.reap_pods(api, _cfg(reaper), "gitlab-runner", NOW,
                           never_over_budget, log=lambda *a, **k: None)
    assert not [m for m, _p, _b in api.calls if m == "DELETE"]
    assert out.deleted == 0 and out.had_errors is False


def test_reap_pods_fails_the_job_when_the_list_call_fails(reaper):
    class Broken(FakeApi):
        def request(self, method, path, body=None):
            raise urllib.error.URLError("forbidden")

    out = reaper.reap_pods(Broken(), _cfg(reaper), "gitlab-runner", NOW,
                           never_over_budget, log=lambda *a, **k: None)
    assert out.had_errors is True


def test_reap_pods_stops_at_the_budget(reaper):
    api = FakeApi(pages={"/pods": [{"items": [pod(EXECUTOR, uid="u1")]}]})
    out = reaper.reap_pods(api, _cfg(reaper), "gitlab-runner", NOW,
                           lambda: True, log=lambda *a, **k: None)
    assert out.budget_hit is True and out.deleted == 0
    assert not [m for m, _p, _b in api.calls if m == "DELETE"]


def test_pod_listing_follows_the_continue_token(reaper):
    api = FakeApi(pages={"/pods": [
        {"items": [pod(EXECUTOR, uid="u1")], "metadata": {"continue": "tok"}},
        {"items": [pod(EXECUTOR + "-2", uid="u2")]},
    ]})
    listed = list(reaper.iter_terminal_pods(api, "gitlab-runner", "Succeeded", 25,
                                            "example.test/runner-class"))
    assert [p["metadata"]["uid"] for p in listed] == ["u1", "u2"]
    assert len(api.raw_paths) == 2
    assert "continue=tok" in api.raw_paths[1]
    # The label + phase selectors must survive the second page, or it would
    # return every pod in the namespace.
    assert "labelSelector=" in api.raw_paths[1] and "status.phase%3DSucceeded" in api.raw_paths[1]


# --- secret reaping --------------------------------------------------------

def test_reap_secrets_keeps_referenced_young_and_non_runner_secrets(reaper):
    api = FakeApi(pages={
        "/pods": [{"items": [
            {"metadata": {"uid": "live-1"},
             "spec": {"imagePullSecrets": [{"name": executor_secret("pulled")}]}}]}],
        "/secrets": [{"items": [
            secret(executor_secret("pulled"), uid="s1"),
            secret(executor_secret("owned"), uid="s2", owner_uid="live-1"),
            secret(executor_secret("young"), uid="s3", age=10),
            secret("gitlab-runner-token", uid="s4"),
            # A user-created registry credential: the 'runner-' prefix alone
            # must not make it eligible, however old it is.
            secret("runner-registry", uid="s5", age=10000),
            secret(executor_secret("leaked"), uid="s6", age=1000),
        ]}],
    })
    out = reaper.reap_secrets(api, _cfg(reaper), "gitlab-runner", NOW,
                              never_over_budget, log=lambda *a, **k: None)
    deleted = [p.rsplit("/", 1)[-1] for m, p, _b in api.calls if m == "DELETE"]
    assert deleted == [executor_secret("leaked")]
    assert out.deleted == 1 and out.had_errors is False


def test_reap_secrets_lists_only_dockercfg_secrets(reaper):
    api = FakeApi(pages={"/secrets": [{"items": []}]})
    list(reaper.iter_dockercfg_secrets(api, "gitlab-runner", 25))
    assert api.calls == [("GET", "/api/v1/namespaces/gitlab-runner/secrets", None)]
    # The server-side type filter is what keeps this ServiceAccount from ever
    # reading the runner registration token or the SA-token secrets, so assert
    # it reaches the API — the bare path above passes with or without it.
    assert api.raw_paths == [
        "/api/v1/namespaces/gitlab-runner/secrets"
        "?fieldSelector=type%3Dkubernetes.io%2Fdockercfg&limit=25"
    ]


def test_reap_secrets_keeps_a_secret_whose_age_is_unknown(reaper):
    # No/unparseable creationTimestamp is the KEEP branch: an in-flight job's
    # credentials must never be deleted because its clock could not be read.
    stale = secret(executor_secret("leaked"), uid="s1", age=1000)
    del stale["metadata"]["creationTimestamp"]
    unparseable = secret(executor_secret("other"), uid="s2")
    unparseable["metadata"]["creationTimestamp"] = "nope"
    api = FakeApi(pages={"/pods": [{"items": []}],
                         "/secrets": [{"items": [stale, unparseable]}]})
    out = reaper.reap_secrets(api, _cfg(reaper), "gitlab-runner", NOW,
                              never_over_budget, log=lambda *a, **k: None)
    assert not [m for m, _p, _b in api.calls if m == "DELETE"]
    assert out.deleted == 0 and out.had_errors is False


@pytest.mark.parametrize("code,errors", [(404, False), (409, False), (500, True)])
def test_reap_secrets_treats_races_as_no_ops_and_real_errors_as_failures(reaper, code, errors):
    leaked = executor_secret("leaked")
    api = FakeApi(
        pages={"/pods": [{"items": []}],
               "/secrets": [{"items": [secret(leaked, uid="s1", age=1000)]}]},
        deletes={leaked: http_error(code)})
    out = reaper.reap_secrets(api, _cfg(reaper), "gitlab-runner", NOW,
                              never_over_budget, log=lambda *a, **k: None)
    assert out.had_errors is errors and out.deleted == 0


def test_reap_secrets_counts_a_transport_error_on_delete(reaper):
    leaked = executor_secret("leaked")
    api = FakeApi(
        pages={"/pods": [{"items": []}],
               "/secrets": [{"items": [secret(leaked, uid="s1", age=1000)]}]},
        deletes={leaked: urllib.error.URLError("connection reset")})
    out = reaper.reap_secrets(api, _cfg(reaper), "gitlab-runner", NOW,
                              never_over_budget, log=lambda *a, **k: None)
    assert out.had_errors is True and out.deleted == 0


def test_live_pod_refs_collects_uids_and_pull_secrets(reaper):
    api = FakeApi(pages={"/pods": [{"items": [
        {"metadata": {"uid": "a"}, "spec": {"imagePullSecrets": [{"name": "s1"}]}},
        {"metadata": {"uid": "b"}, "spec": {}},
    ]}]})
    assert reaper.live_pod_refs(api, "gitlab-runner", 25,
                                never_over_budget) == ({"a", "b"}, {"s1"})


def test_live_pod_refs_gives_up_rather_than_return_a_partial_set(reaper):
    # The ref set is what proves a Secret unreferenced; half of it proves
    # nothing. None (not the partial set) is the only safe answer.
    api = FakeApi(pages={"/pods": [
        {"items": [{"metadata": {"uid": "a"}, "spec": {}}], "metadata": {"continue": "tok"}},
        {"items": [{"metadata": {"uid": "b"}, "spec": {}}]},
    ]})
    assert reaper.live_pod_refs(api, "gitlab-runner", 25, lambda: True) is None
    # And it stops BEFORE spending the next request, which is the point of the
    # budget: one page listed, the second never asked for.
    assert len(api.raw_paths) == 1


def test_live_pod_refs_completes_when_the_budget_dies_on_the_last_page(reaper):
    # No continue token -> the set is COMPLETE, so an over-budget clock must not
    # cost the namespace its sweep; the per-secret budget check ends the run.
    api = FakeApi(pages={"/pods": [{"items": [{"metadata": {"uid": "a"}, "spec": {}}]}]})
    assert reaper.live_pod_refs(api, "gitlab-runner", 25, lambda: True) == ({"a"}, set())


def test_reap_secrets_skips_the_whole_sweep_when_the_live_listing_is_cut_short(reaper):
    # An incomplete live-ref set must never feed deletions: the namespace's
    # secret sweep is abandoned entirely, not run against what was read.
    api = FakeApi(pages={
        "/pods": [
            {"items": [], "metadata": {"continue": "tok"}},
            {"items": []},
        ],
        "/secrets": [{"items": [secret(executor_secret("leaked"), uid="s1", age=1000)]}],
    })
    out = reaper.reap_secrets(api, _cfg(reaper), "gitlab-runner", NOW,
                              lambda: True, log=lambda *a, **k: None)
    assert out.budget_hit is True and out.deleted == 0
    assert not [m for m, _p, _b in api.calls if m == "DELETE"]
    # The secrets were never even listed — the sweep did not start.
    assert not [p for _m, p, _b in api.calls if p.endswith("/secrets")]


# --- run() -----------------------------------------------------------------

def test_run_exits_non_zero_only_on_a_real_error(reaper):
    clean = FakeApi(pages={"/pods": [{"items": []}], "/secrets": [{"items": []}]})
    assert reaper.run(clean, _cfg(reaper), NOW, never_over_budget,
                      log=lambda *a, **k: None) == 0

    broken = FakeApi(pages={"/pods": [{"items": [pod(EXECUTOR, uid="u1")]}],
                            "/secrets": [{"items": []}]},
                     deletes={EXECUTOR: http_error(500)})
    assert reaper.run(broken, _cfg(reaper), NOW, never_over_budget,
                      log=lambda *a, **k: None) == 1


def test_run_exits_zero_on_a_clean_budget_stop(reaper):
    api = FakeApi(pages={"/pods": [{"items": [pod(EXECUTOR, uid="u1")]}]})
    assert reaper.run(api, _cfg(reaper), NOW, lambda: True,
                      log=lambda *a, **k: None) == 0


def test_budget_stop_leaves_the_later_namespaces_untouched_and_says_so(reaper):
    # Exit 0 makes the log line the only evidence the sweep was partial.
    cfg = _cfg(reaper, NAMESPACES="gitlab-runner-privileged gitlab-runner")
    order = reaper.rotate_namespaces(cfg.namespaces, NOW)
    api = FakeApi(pages={"/pods": [{"items": [pod(EXECUTOR, uid="u1")]}],
                         "/secrets": [{"items": []}]})
    lines = []
    assert reaper.run(api, cfg, NOW, lambda: True,
                      log=lambda msg, **k: lines.append(msg)) == 0
    assert {p.split("/")[4] for _m, p, _b in api.calls} == {order[0]}
    stop = [line for line in lines if line.startswith("BUDGET STOP")]
    assert len(stop) == 1 and order[1] in stop[0]
    assert any("budget_stop=yes" in line for line in lines)


def test_budget_stop_in_the_last_namespace_still_reports_a_partial_run(reaper):
    # The stop lands in the FINAL namespace, so nothing follows it: reporting
    # only the namespaces AFTER the stop would log nothing and claim
    # budget_stop=no, and rotation reaches this shape on a schedule.
    cfg = _cfg(reaper, NAMESPACES="gitlab-runner-privileged gitlab-runner")
    order = reaper.rotate_namespaces(cfg.namespaces, NOW, cfg.rotate_period_seconds)

    class OnlyTheLastNamespaceHasWork(FakeApi):
        # The first namespace lists nothing, so over_budget is never consulted
        # there and it completes; the budget is hit on the last one's first pod.
        def request(self, method, path, body=None):
            self.calls.append((method, path.split("?")[0], body))
            self.raw_paths.append(path)
            if f"/namespaces/{order[-1]}/pods" in path:
                return {"items": [pod(EXECUTOR, uid="u1")]}
            return {"items": []}

    api = OnlyTheLastNamespaceHasWork()
    lines = []
    assert reaper.run(api, cfg, NOW, lambda: True,
                      log=lambda msg, **k: lines.append(msg)) == 0
    stop = [line for line in lines if line.startswith("BUDGET STOP")]
    assert len(stop) == 1
    assert stop[0].split(": ")[-1].split() == [order[-1]]
    assert any("budget_stop=yes" in line for line in lines)


def test_a_cut_short_live_pod_listing_reports_a_budget_stop(reaper):
    # The secret sweep is skipped without a single secret being listed, so the
    # only evidence the namespace was not swept is the BUDGET STOP line.
    cfg = _cfg(reaper)

    class LiveListingHasAnotherPage(FakeApi):
        # The terminal-phase listings are empty (so reap_pods completes and never
        # consults the budget); the unfiltered live-pod listing has a continue
        # token, which is where the budget stop lands.
        def request(self, method, path, body=None):
            self.calls.append((method, path.split("?")[0], body))
            self.raw_paths.append(path)
            if "/pods" in path and "status.phase" not in path:
                return {"items": [], "metadata": {"continue": "tok"}}
            return {"items": []}

    api = LiveListingHasAnotherPage()
    lines = []
    assert reaper.run(api, cfg, NOW, lambda: True,
                      log=lambda msg, **k: lines.append(msg)) == 0
    assert not [m for m, _p, _b in api.calls if m == "DELETE"]
    stop = [line for line in lines if line.startswith("BUDGET STOP")]
    assert len(stop) == 1 and stop[0].split(": ")[-1].split() == cfg.namespaces
    assert any("budget_stop=yes" in line for line in lines)


def test_a_complete_run_reports_no_budget_stop(reaper):
    api = FakeApi(pages={"/pods": [{"items": []}], "/secrets": [{"items": []}]})
    lines = []
    reaper.run(api, _cfg(reaper), NOW, never_over_budget,
               log=lambda msg, **k: lines.append(msg))
    assert not [line for line in lines if line.startswith("BUDGET STOP")]
    assert any("budget_stop=no" in line for line in lines)


def test_namespace_start_rotates_between_scheduled_runs(reaper):
    # Without rotation a namespace with a standing backlog starves every other
    # one forever, because a budget stop always lands in the same place.
    namespaces = ["ns-a", "ns-b", "ns-c"]
    heads = [reaper.rotate_namespaces(namespaces, NOW + timedelta(minutes=15 * i))[0]
             for i in range(len(namespaces))]
    assert set(heads) == set(namespaces)
    for i in range(len(namespaces)):
        rotated = reaper.rotate_namespaces(namespaces, NOW + timedelta(minutes=15 * i))
        assert sorted(rotated) == sorted(namespaces)
    assert reaper.rotate_namespaces([], NOW) == []
