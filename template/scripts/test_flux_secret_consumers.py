"""Unit tests for scripts/flux-secret-consumers.py.

Each test feeds the helper a throwaway `kubectl get -o json` payload on stdin
and asserts the TSV it emits, or the exit code it refuses with.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parent / "flux-secret-consumers.py"


def _workload(name, kind="Deployment", managed=False, volumes=None, env_from=None,
              env=None, pull=None, match_labels=None):
    labels = {"kustomize.toolkit.fluxcd.io/name": "apps"} if managed else {}
    selector = {} if match_labels is None else {"matchLabels": match_labels}
    if match_labels is None:
        selector = {"matchLabels": {"app.kubernetes.io/name": name}}
    container = {"name": name}
    if env_from:
        container["envFrom"] = [{"secretRef": {"name": s}} for s in env_from]
    if env:
        container["env"] = [
            {"name": "X", "valueFrom": {"secretKeyRef": {"name": s, "key": "k"}}}
            for s in env
        ]
    pod_spec = {"containers": [container]}
    if volumes:
        pod_spec["volumes"] = [{"name": s, "secret": {"secretName": s}} for s in volumes]
    if pull:
        pod_spec["imagePullSecrets"] = [{"name": s} for s in pull]
    return {
        "kind": kind,
        "metadata": {"name": name, "labels": labels},
        "spec": {"selector": selector, "template": {"spec": pod_spec}},
    }


def _run(items, secret):
    payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
    return subprocess.run(
        [sys.executable, str(HELPER), secret],
        input=payload, capture_output=True, text=True, check=False,
    )


def test_every_reference_mechanism_is_a_consumer():
    items = [
        _workload("a", volumes=["app-secrets"], managed=True),
        _workload("b", kind="StatefulSet", env_from=["app-secrets"]),
        _workload("c", kind="DaemonSet", env=["app-secrets"], managed=True),
        _workload("d", pull=["app-secrets"]),
    ]
    run = _run(items, "app-secrets")
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "deployment/a\tkustomize\tapp.kubernetes.io/name=a",
        "statefulset/b\tother\tapp.kubernetes.io/name=b",
        "daemonset/c\tkustomize\tapp.kubernetes.io/name=c",
        "deployment/d\tother\tapp.kubernetes.io/name=d",
    ]


def test_a_non_consumer_is_not_listed():
    """Mutation case: a namespace-wide restart would sweep this workload in."""
    items = [
        _workload("consumer", env_from=["app-secrets"]),
        _workload("bystander", env_from=["other-secrets"], volumes=["tls"]),
    ]
    run = _run(items, "app-secrets")
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["deployment/consumer\tother\tapp.kubernetes.io/name=consumer"]


def test_no_consumer_prints_nothing_and_exits_clean():
    run = _run([_workload("bystander", env_from=["other-secrets"])], "app-secrets")
    assert run.returncode == 0
    assert run.stdout == ""


def test_a_consumer_with_no_match_labels_is_refused():
    run = _run([_workload("a", env_from=["app-secrets"], match_labels={})], "app-secrets")
    assert run.returncode == 2
    assert "no matchLabels on deployment/a" in run.stderr


def test_input_that_is_not_a_kubectl_list_is_refused():
    broken = subprocess.run(
        [sys.executable, str(HELPER), "app-secrets"],
        input="not json", capture_output=True, text=True, check=False,
    )
    assert broken.returncode == 2
    assert "not the JSON kubectl emits" in broken.stderr
    bare = subprocess.run(
        [sys.executable, str(HELPER), "app-secrets"],
        input="{}", capture_output=True, text=True, check=False,
    )
    assert bare.returncode == 2
    assert "carries no .items" in bare.stderr
