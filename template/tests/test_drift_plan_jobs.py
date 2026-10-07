"""The drift-plan jobs detect a console edit; they never reconcile one.

Each plans read-only and leans on exit code 2, so an added apply or a plan
without `-detailed-exitcode` makes it an unsupervised apply or always green.
"""

from __future__ import annotations

import re

import pytest
import yaml
from conftest import REPO

CI_FILE = REPO / ".gitlab-ci.yml"
APPLY_RE = re.compile(r"\bterraform\s+apply\b")

needs_pipeline = pytest.mark.skipif(not CI_FILE.is_file(), reason="no .gitlab-ci.yml")


class _CILoader(yaml.SafeLoader):
    """SafeLoader tolerating GitLab's `!reference` tags, subclassed so the
    constructor is not registered on the global SafeLoader."""


_CILoader.add_multi_constructor(
    "!",
    lambda loader, suffix, node: (
        loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.SequenceNode)
        else None
    ),
)


def _pipeline() -> dict:
    """The jobs document: GitLab's inputs syntax makes a pipeline two documents,
    `spec:` then the jobs, so the last mapping document is the one to read."""
    if not CI_FILE.is_file():
        return {}
    docs = [
        doc
        for doc in yaml.load_all(CI_FILE.read_text(), Loader=_CILoader)
        if isinstance(doc, dict)
    ]
    return docs[-1] if docs else {}


def _drift_jobs(pipeline: dict) -> dict[str, dict]:
    """Every rendered drift-plan job. The set is answer-driven: the tailscale and
    unifi jobs render only when those answers enable them. A leading dot marks a
    hidden `extends:` template, which runs nothing of its own."""
    return {
        name: job
        for name, job in pipeline.items()
        if isinstance(job, dict)
        and name.endswith("-drift-plan")
        and not name.startswith(".")
    }


def _inherited(pipeline: dict, job: dict, key: str):
    """A job's value for `key`, following its `extends:` parents when it sets none.

    The shared shape lives in `.terraform-drift-plan`, so reading the job mapping
    alone would miss the contract every job inherits from it.
    """
    if key in job:
        return job[key]
    parents = job.get("extends") or []
    parents = [parents] if isinstance(parents, str) else parents
    for parent in parents:
        candidate = pipeline.get(parent)
        if isinstance(candidate, dict):
            value = _inherited(pipeline, candidate, key)
            if value is not None:
                return value
    return None


def _script(job: dict) -> list[str]:
    raw = job.get("script") or []
    return [str(line) for line in ([raw] if isinstance(raw, str) else raw)]


def _applies(script: list[str]) -> list[str]:
    return [line for line in script if APPLY_RE.search(line)]


PIPELINE = _pipeline()
DRIFT_JOBS = _drift_jobs(PIPELINE)


@needs_pipeline
def test_the_pipeline_declares_drift_plan_jobs():
    """A renamed job would make every parametrised case below vacuous."""
    assert DRIFT_JOBS, (
        "no `*-drift-plan` job in .gitlab-ci.yml — the supervised-apply roots "
        "then have nothing watching them for an admin-console edit"
    )


@pytest.mark.parametrize("name", sorted(DRIFT_JOBS))
def test_a_drift_plan_never_applies(name):
    """The jobs' central claim: reconciling stays a supervised apply."""
    offenders = _applies(_script(DRIFT_JOBS[name]))
    assert not offenders, (
        f"{name} applies rather than planning:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name", sorted(DRIFT_JOBS))
def test_a_drift_plan_asks_for_a_detailed_exit_code(name):
    """Without it the tolerated exit code 2 never happens and drift reads green."""
    plans = [line for line in _script(DRIFT_JOBS[name]) if "terraform plan" in line]
    assert plans, f"{name} runs no `terraform plan`"
    missing = [line for line in plans if "-detailed-exitcode" not in line]
    assert not missing, (
        f"{name} plans without -detailed-exitcode, so its `allow_failure: "
        "exit_codes: [2]` contract can never fire:\n  " + "\n  ".join(missing)
    )


@pytest.mark.parametrize("name", sorted(DRIFT_JOBS))
def test_only_the_drift_exit_code_is_tolerated(name):
    """`allow_failure: true` would swallow a broken plan as well as drift."""
    allow_failure = _inherited(PIPELINE, DRIFT_JOBS[name], "allow_failure")
    assert allow_failure == {"exit_codes": [2]}, (
        f"{name} declares allow_failure {allow_failure!r}; only exit code 2, the "
        "drift signal, is advisory"
    )


def _resolved_rules(pipeline: dict, job: dict) -> list:
    """A job's rules with every `!reference` expanded.

    The loader renders `!reference [a, b]` as the list `["a", "b"]`, so an
    unexpanded entry hides whatever the referenced template actually allows.
    """
    out: list = []
    for entry in _inherited(pipeline, job, "rules") or []:
        if not isinstance(entry, list):
            out.append(entry)
            continue
        target = pipeline
        for key in entry:
            target = target.get(key) if isinstance(target, dict) else None
        out.extend(target or [])
    return out


def _rule_conditions(rules: list) -> list[str]:
    return [str(rule.get("if", "")) for rule in rules if isinstance(rule, dict)]


@pytest.mark.parametrize("name", sorted(DRIFT_JOBS))
def test_a_drift_plan_never_runs_on_a_merge_request(name):
    """CRITICAL: the job materializes vault credentials into its own shell, so it
    must never run a branch nobody has reviewed."""
    offenders = [
        condition
        for condition in _rule_conditions(_resolved_rules(PIPELINE, DRIFT_JOBS[name]))
        if "merge_request_event" in condition
    ]
    assert not offenders, (
        f"{name} reads provider credentials on a merge-request pipeline:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name", sorted(DRIFT_JOBS))
def test_the_scheduled_detector_carries_no_credential_guard(name):
    """A falsy guard REMOVES the job, so a revoked token would hide drift behind
    a green pipeline instead of reddening the plan."""
    conditions = _rule_conditions(_resolved_rules(PIPELINE, DRIFT_JOBS[name]))
    scheduled = [c for c in conditions if 'CI_PIPELINE_SOURCE == "schedule"' in c]
    assert scheduled, (
        f"{name} has no scheduled rule, so an admin-console edit made while "
        "nothing in-repo changed is never detected"
    )
    guarded = [c for c in scheduled if "&&" in c]
    assert not guarded, (
        f"{name}'s scheduled rule is gated on another variable; a falsy guard "
        "removes the detector rather than failing it:\n  " + "\n  ".join(guarded)
    )


def test_the_reference_expansion_is_load_bearing():
    """Mutation case: an unexpanded reference would read as a rule-free job."""
    pipeline = {
        ".shared": {"rules": [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}]},
        "x-drift-plan": {"rules": [[".shared", "rules"]]},
    }
    assert _rule_conditions(_resolved_rules(pipeline, pipeline["x-drift-plan"])) == [
        '$CI_PIPELINE_SOURCE == "merge_request_event"'
    ]


def test_the_apply_detection_is_load_bearing():
    """Mutation case: an apply added to a drift-plan script must be reported."""
    assert _applies(["terraform plan -detailed-exitcode"]) == []
    assert _applies(["terraform apply -auto-approve"]) != []
    assert _applies(["terraform  apply tfplan"]) != []
