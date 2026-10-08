"""scripts/flux-corpus-gates.sh is the single gate list `task flux:lint` and the
CI flux-lint job both run, so a finding must fail it and no gate may be skipped
because an earlier one failed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATES = REPO / "scripts" / "flux-corpus-gates.sh"

# One PVC with no storageClassName: the finding check-pvc-storageclass.py exists
# to catch. The backup-artifact gate runs after it, so its header proves the
# run did not stop.
CORPUS_WITH_A_FINDING = """\
apiVersion: v1
kind: Namespace
metadata:
  name: demo
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
  namespace: demo
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
"""

GATE_HEADERS = (
    "Checking HPA/VPA invariant",
    "Checking scrape/NetworkPolicy invariant",
    "Checking ingress default-deny coverage",
    "Checking ClusterSecretStore scoping",
    "Checking PVC storageClassName",
    "Checking NFS PersistentVolume TLS",
    "Checking backup-artifact apps against their alert arms",
)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GATES), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    path = tmp_path / "render-all.yaml"
    path.write_text(CORPUS_WITH_A_FINDING)
    return path


def test_the_wrapper_ships_executable() -> None:
    assert GATES.is_file(), "the corpus-gate wrapper is missing"
    assert os.access(GATES, os.X_OK), f"{GATES.name} is not executable"


def test_a_corpus_with_a_finding_fails(corpus: Path) -> None:
    """Non-zero, not exactly 1: a hand-written corpus this small also trips the
    other gates' own "checked nothing" guards, which are exit 2. The stub-tree
    cases below pin 1 against 2."""
    result = run(str(corpus))
    assert result.returncode != 0, f"{result.stdout}{result.stderr}"
    assert "no storageClassName" in result.stdout + result.stderr


def test_every_gate_still_runs_after_one_fails(corpus: Path) -> None:
    result = run(str(corpus))
    missing = [header for header in GATE_HEADERS if header not in result.stdout]
    assert not missing, f"gates skipped after an earlier failure: {missing}"


def test_the_last_document_of_the_corpus_is_still_inspected(corpus: Path) -> None:
    """An arm that appends another tree needs its own separator, or the corpus's
    final document merges into the first appended one and vanishes."""
    result = run(str(corpus))
    assert "demo/PersistentVolumeClaim/data" in result.stdout + result.stderr


def test_a_tenant_only_resource_reaches_the_corpus(corpus: Path) -> None:
    """The tenants tree carries no spec.path, so the caller's per-Kustomization
    loop never renders it and the wrapper has to append it itself."""
    result = run(str(corpus))
    assert "tenants to the corpus" in result.stdout, f"{result.stdout}{result.stderr}"
    assert "tenant-crd-editor" in corpus.read_text(), (
        "the tenants tree never reached the corpus the gates read, so a tenant's "
        "own SecretStore or NetworkPolicy is ungated"
    )


def test_a_missing_corpus_is_an_operator_error(tmp_path: Path) -> None:
    assert run().returncode == 2
    assert run(str(tmp_path / "absent.yaml")).returncode == 2


def test_a_tree_with_no_cluster_is_an_operator_error(tmp_path: Path) -> None:
    """Mutation case: with an unmatched glob the loops skip the literal pattern
    and the gates exit 0 over a corpus missing the trees they exist to cover."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(GATES, scripts / GATES.name)
    corpus = tmp_path / "render-all.yaml"
    corpus.write_text(CORPUS_WITH_A_FINDING)
    result = subprocess.run(
        ["bash", str(scripts / GATES.name), str(corpus)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "inspected no cluster" in result.stderr


def test_an_empty_corpus_is_an_operator_error(tmp_path: Path) -> None:
    """Mutation case: with no object in the corpus every gate below reports a
    clean run, so the whole stage passes on a render that produced nothing."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(GATES, scripts / GATES.name)
    cluster = tmp_path / "kubernetes" / "clusters" / "demo" / "tenants"
    cluster.mkdir(parents=True)
    (cluster / "kustomization.yaml").write_text("resources: []\n")
    corpus = tmp_path / "render-all.yaml"
    corpus.write_text("")
    result = subprocess.run(
        ["bash", str(scripts / GATES.name), str(corpus)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "holds no Kubernetes object" in result.stderr


_HPA_VPA_GATE = "check-hpa-vpa-invariant.py"

# A VPA capping memory on a target the corpus renders no limit for: the shape
# every chart-rendered workload has here, and the one the gate will not judge.
CORPUS_WITH_AN_UNJUDGED_CAP = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
  namespace: demo
spec:
  template:
    spec:
      containers:
        - name: app
          image: example/app:1
---
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: app
  namespace: demo
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: app
  updatePolicy:
    updateMode: "Off"
  resourcePolicy:
    containerPolicies:
      - containerName: app
        maxAllowed:
          memory: 1Gi
"""


def _hpa_vpa_flags() -> list[str]:
    """The flags the wrapper passes the HPA/VPA gate, read from the script.

    Read rather than restated, so a flag dropped from the wrapper fails the
    cases below instead of only changing what they exercise.
    """
    text = GATES.read_text(encoding="utf-8").replace("\\\n", " ")
    marker = f"python3 scripts/{_HPA_VPA_GATE}"
    assert marker in text, f"the wrapper no longer runs {_HPA_VPA_GATE}"
    call = text.split(marker, 1)[1].split("||", 1)[0]
    return [word for word in call.split() if word.startswith("--") or word.endswith(".yaml")]


def _pointed_at(flags: list[str], policy: Path) -> list[str]:
    """The same flags against a policy declaring no chart-native target, so only
    the cap arm can decide the exit code."""
    swapped = list(flags)
    assert "--policy-config" in swapped, "the wrapper passes the gate no --policy-config"
    swapped[swapped.index("--policy-config") + 1] = str(policy)
    return swapped


def _run_hpa_vpa(flags: list[str], corpus: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / _HPA_VPA_GATE), *flags],
        cwd=REPO,
        input=corpus,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def minimal_policy(tmp_path: Path) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text("chart_native_hpa_targets: []\n")
    return path


def test_the_wrapper_excuses_a_cap_the_corpus_cannot_judge(minimal_policy: Path) -> None:
    """A chart renders most VPA targets here, so the corpus carries no limit to
    compare their cap against. validate-helm-values.py judges those against the
    chart-rendered limits, so the corpus wrapper must not red on them."""
    result = _run_hpa_vpa(
        _pointed_at(_hpa_vpa_flags(), minimal_policy), CORPUS_WITH_AN_UNJUDGED_CAP
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert "NOT JUDGED" in result.stderr, (
        "the gate judged the cap after all, so this case no longer covers the "
        "acknowledgement the wrapper passes"
    )


def test_an_unjudged_cap_reds_without_the_acknowledgement(minimal_policy: Path) -> None:
    """Mutation case: the flag is what excuses the cap, so the gate still has a
    finding for a caller that does not pass it."""
    flags = [
        flag
        for flag in _pointed_at(_hpa_vpa_flags(), minimal_policy)
        if flag != "--allow-unjudged-vpa-caps"
    ]
    result = _run_hpa_vpa(flags, CORPUS_WITH_AN_UNJUDGED_CAP)
    assert result.returncode == 1, f"{result.stdout}{result.stderr}"


def _caller_paths(name: str) -> list[Path]:
    """Every file a caller name resolves to.

    `Taskfile.yml` is a tree: the root file plus taskfiles/<ns>.yml. The `.jinja`
    fallback is the un-rendered template tree.
    """
    candidates = [REPO / name, REPO / f"{name}.jinja"]
    if name == "Taskfile.yml":
        candidates += sorted((REPO / "taskfiles").glob("*.yml"))
        candidates += sorted((REPO / "taskfiles").glob("*.yml.jinja"))
    return [path for path in candidates if path.is_file()]


def test_both_callers_pass_the_versions_configmap() -> None:
    """Without the second argument the HelmRelease values validation is skipped,
    so the two real call sites are asserted to supply it."""
    for name in ("Taskfile.yml", ".gitlab-ci.yml"):
        paths = _caller_paths(name)
        assert paths, f"{name} is missing"
        calls = [
            line
            for path in paths
            for line in path.read_text().splitlines()
            if "bash scripts/flux-corpus-gates.sh" in line
        ]
        assert calls, f"{name} no longer calls scripts/flux-corpus-gates.sh"
        for call in calls:
            argv = call.split("bash scripts/flux-corpus-gates.sh", 1)[1].split("||", 1)[0]
            assert len(argv.split()) >= 2, (
                f"{name} calls the wrapper without a versions ConfigMap, which silently "
                f"skips the HelmRelease values validation: {call.strip()}"
            )


PROM_LINT = "scripts/lint-prometheus-config.sh"
RULE_TESTS_DIR = "tests/prometheus-rules"


def test_every_caller_points_the_prometheus_lint_at_the_rule_tests() -> None:
    """The vendored script defaults RULE_TESTS_DIR to a path no render has, and
    then skips the promtool alert unit tests with a success exit."""
    for name in ("Taskfile.yml", ".gitlab-ci.yml", "tests/validate_render.py"):
        for path in _caller_paths(name):
            text = path.read_text()
            if PROM_LINT not in text:
                continue
            assert RULE_TESTS_DIR in text, (
                f"{path.name} runs {PROM_LINT} without naming {RULE_TESTS_DIR}, so "
                "the promtool alert unit tests are skipped and the job still passes"
            )

    tests_dir = REPO / RULE_TESTS_DIR
    assert tests_dir.is_dir(), f"{RULE_TESTS_DIR} is missing — every caller names a dead path"
    assert list(tests_dir.glob("*.test.yaml")), (
        f"{RULE_TESTS_DIR} holds no *.test.yaml, so the promtool suite runs nothing"
    )


def _stub_tree(tmp_path: Path, failing: str, code: int) -> Path:
    """A fixture repo where every gate is a stub and one of them exits `code`."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(GATES, scripts / GATES.name)
    for gate in GATES.read_text().split():
        if not gate.startswith("scripts/check-") or not gate.endswith(".py"):
            continue
        rc = code if Path(gate).name == failing else 0
        (tmp_path / gate).write_text(f"import sys\nsys.exit({rc})\n")
    (scripts / "autoscaling-policy.yaml").write_text("{}\n")
    (tmp_path / "kubernetes" / "clusters" / "demo" / "tenants").mkdir(parents=True)
    (tmp_path / "kubernetes" / "clusters" / "demo" / "tenants" / "kustomization.yaml").write_text(
        "resources: []\n"
    )
    corpus = tmp_path / "render-all.yaml"
    corpus.write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: demo\n")
    return corpus


def _run_in(tmp_path: Path, corpus: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(tmp_path / "scripts" / GATES.name), str(corpus)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_gates_operator_error_is_not_collapsed_into_a_finding(tmp_path: Path) -> None:
    """Mutation case: a gate exiting 2 means it could not judge the corpus. Reported
    as 1 it reads as a policy finding, and a retry or an exemption is the wrong fix."""
    corpus = _stub_tree(tmp_path, "check-pvc-storageclass.py", 2)
    result = _run_in(tmp_path, corpus)
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "check-pvc-storageclass.py exited 2" in result.stderr
    assert "OPERATOR ERROR" in result.stderr


def test_a_gates_finding_still_exits_one(tmp_path: Path) -> None:
    corpus = _stub_tree(tmp_path, "check-pvc-storageclass.py", 1)
    result = _run_in(tmp_path, corpus)
    assert result.returncode == 1, f"{result.stdout}{result.stderr}"
    assert "OPERATOR ERROR" not in result.stderr


def test_a_clean_run_exits_zero(tmp_path: Path) -> None:
    """The documented one-argument form: every gate passes and the HelmRelease
    values validation is the documented skip, so the wrapper must exit 0."""
    corpus = _stub_tree(tmp_path, "", 0)
    result = _run_in(tmp_path, corpus)
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    missing = [header for header in GATE_HEADERS if header not in result.stdout]
    assert not missing, f"gates that never ran on a clean corpus: {missing}"


def test_an_empty_merged_configmap_is_an_operator_error(tmp_path: Path) -> None:
    """Mutation case: with no substitutions every `${...}` stays unresolved and
    the HelmRelease values validation below still reports a clean run."""
    corpus = _stub_tree(tmp_path, "", 0)
    scripts = tmp_path / "scripts"
    flux_env = scripts / "flux-env.sh"
    flux_env.write_text("#!/usr/bin/env bash\nexit 0\n")
    flux_env.chmod(0o755)
    (scripts / "validate-helm-values.py").write_text("import sys\nsys.exit(0)\n")
    (scripts / "helm-values-releases.yaml").write_text("releases: []\n")
    versions = tmp_path / "versions-configmap.yaml"
    versions.write_text("data: {}\n")
    result = subprocess.run(
        ["bash", str(scripts / GATES.name), str(corpus), str(versions)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, f"{result.stdout}{result.stderr}"
    assert "empty merged configmap" in result.stderr
