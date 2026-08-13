"""Render the template with the fixture answers and assert what must be true
of EVERY generated cluster.

Four families:

* the render happens at all, with the answers recorded for `copier update`;
* no value from the reference cluster survives into the output;
* the Kubernetes tree carries substitution placeholders, not site literals;
* Ansible reaches its roles by FQCN from the pinned collection, never from a
  vendored `roles/` directory.

The generated repository ships invariants of its own (tests/), and this suite
runs them too — a cluster that fails its own gate must not be generatable.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

import render_cluster

REPO_ROOT = render_cluster.REPO_ROOT

# Reference-cluster literals that must never appear in a generated repository.
# The exception is a line that also names an upstream repository: those live on
# the reference cluster's GitLab, and a pinned collection/module source or a
# doc link legitimately points there wherever the generated cluster lives.
FORBIDDEN = ("esweiss.com", "ericsweiss.com")
UPSTREAM_REPOS = ("weisssrv-lib", "weisssrv-cluster-template", "weisssrv-app-template")
TEXT_SUFFIXES = {
    ".yml", ".yaml", ".md", ".py", ".sh", ".tf", ".cfg", ".toml", ".json",
    ".j2", ".jinja", ".txt", ".hujson", ".env",
}


@pytest.fixture(scope="session")
def answers() -> dict:
    return yaml.safe_load(render_cluster.ANSWERS.read_text())


@pytest.fixture(scope="session")
def rendered(tmp_path_factory) -> Path:
    scratch = tmp_path_factory.mktemp("render")
    return render_cluster.render(scratch)


ANSWERS_B = REPO_ROOT / "tests" / "answers-unlike.yml"


@pytest.fixture(scope="session")
def answers_b() -> dict:
    return yaml.safe_load(ANSWERS_B.read_text())


@pytest.fixture(scope="session")
def rendered_b(tmp_path_factory) -> Path:
    """A second render from deliberately unlike answers, with both optional
    modules off. It is what makes a hardcoded value visible: in the shaped
    fixture a literal carried over from another cluster renders identically to a
    correct substitution."""
    scratch = tmp_path_factory.mktemp("render-b")
    return render_cluster.render(scratch, answers=ANSWERS_B, dest_name="render-b")


@dataclass(frozen=True)
class Cluster:
    """One rendered repository plus the answers that produced it."""

    label: str
    path: Path
    answers: dict


# Everything that is not ABOUT the difference between the two fixtures runs
# against BOTH. Fixture B is the only render that reaches the both-modules-off
# branches, a 5-host roster, a single resolver and a non-/24-shaped LAN; a suite
# that asserts the cluster's invariants on fixture A alone leaves all of that
# checked by nothing but a Jinja-leftover scan. Rendering is session-scoped, so
# the second parameter costs assertions, not renders.
@pytest.fixture(scope="session", params=["shaped", "unlike"])
def cluster(request) -> Cluster:
    render_fixture, answers_fixture = {
        "shaped": ("rendered", "answers"),
        "unlike": ("rendered_b", "answers_b"),
    }[request.param]
    return Cluster(
        request.param,
        request.getfixturevalue(render_fixture),
        request.getfixturevalue(answers_fixture),
    )


def _text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name.startswith(".")):
            try:
                yield path, path.read_text()
            except (UnicodeDecodeError, OSError):
                continue


def _k8s_files(root: Path):
    k8s = root / "kubernetes"
    if not k8s.is_dir():
        return
    for path in sorted(k8s.rglob("*.yaml")):
        if path.is_file():
            yield path, path.read_text()


_load_ci = render_cluster.load_ci


def _cluster_config(root: Path) -> tuple[Path, dict[str, str]]:
    sources = root / "kubernetes" / "infrastructure" / "sources"
    for path in sorted(sources.glob("*.yaml")) if sources.is_dir() else []:
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and (doc.get("metadata") or {}).get("name") == "cluster-config":
                return path, {k: str(v) for k, v in (doc.get("data") or {}).items()}
    pytest.fail("no cluster-config ConfigMap in kubernetes/infrastructure/sources/")


# --------------------------------------------------------------------------
# The render itself
# --------------------------------------------------------------------------


def test_render_produces_a_repository(cluster):
    assert (cluster.path / ".copier-answers.yml").is_file(), (
        "no answers file — copier update would not work"
    )
    assert (cluster.path / ".gitlab-ci.yml").is_file()


def test_answers_file_records_the_fixture(cluster):
    recorded = yaml.safe_load((cluster.path / ".copier-answers.yml").read_text())
    assert recorded["cluster_name"] == cluster.answers["cluster_name"]
    # lib_ref is inherited from copier.yml's default (the fixture no longer answers
    # it), so copier records the resolved default — `copier update` reproduces it.
    assert recorded.get("lib_ref"), "the answers file must record the resolved lib_ref"


def test_no_unrendered_jinja_statements(cluster):
    """A `{% ... %}` block in the output means a templated file was not given
    the .jinja suffix.

    Scoped to the trees where no OTHER templating language is in play: Ansible
    playbooks embed Jinja by design, and `{{ ... }}` belongs to go-task, Grafana
    dashboards and Prometheus annotations, so neither is evidence of a leak.

    Runs against both fixtures: the `{% if %}` branches the shaped fixture never
    takes (both optional modules off) still have to render.
    """
    root = cluster.path
    scoped = [
        (path, text)
        for path, text in _text_files(root)
        if path.relative_to(root).parts[0] in ("kubernetes", "terraform")
        or path.name == ".gitlab-ci.yml"
    ]
    leftovers = [
        f"{path.relative_to(root)}:{lineno}"
        for path, text in scoped
        for lineno, line in enumerate(text.splitlines(), 1)
        if "{%" in line
    ]
    assert not leftovers, "unrendered Jinja survived the render:\n  " + "\n  ".join(leftovers)


def test_no_answer_survives_as_an_unrendered_expression(cluster):
    """`{{ <question> }}` in the output is a copier answer that was never
    substituted — most often a line written inside a `{% raw %}` block, which
    Taskfile.yml.jinja needs because go-task owns `{{ }}` as well.

    The failure is invisible to the scan above (that one looks for `{%`, and
    skips Taskfile.yml precisely because `{{ }}` is go-task's) yet fatal: go-task
    parses the leftover as a function call and every task in the file dies. Only
    `ansible/` is exempt, where the same names are real Ansible variables.
    """
    config = yaml.safe_load((REPO_ROOT / "copier.yml").read_text())
    names = sorted(k for k in config if not k.startswith("_"))
    leak = re.compile(r"\{\{-?\s*(" + "|".join(names) + r")\s*[|}-]")
    root = cluster.path
    offenders = [
        f"{path.relative_to(root)}:{lineno} {match.group(1)}"
        for path, text in _text_files(root)
        if path.relative_to(root).parts[0] != "ansible"
        for lineno, line in enumerate(text.splitlines(), 1)
        for match in [leak.search(line)]
        if match
    ]
    assert not offenders, (
        "copier answers left unsubstituted in the render:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# No reference-cluster values
# --------------------------------------------------------------------------


def test_no_reference_cluster_literals(cluster):
    offenders = []
    for path, text in _text_files(cluster.path):
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(repo in line for repo in UPSTREAM_REPOS):
                continue
            for needle in FORBIDDEN:
                if needle in line:
                    offenders.append(f"{path.relative_to(cluster.path)}:{lineno} {needle}")
    assert not offenders, (
        "reference-cluster identity leaked into the render — parameterize it:\n  "
        + "\n  ".join(offenders)
    )


def test_the_two_fixtures_answer_differently(answers, answers_b):
    """The contrast fixture only proves anything while its answers differ. A
    key that drifts back into agreement silently disarms the leak check below."""
    assert set(answers) == set(answers_b), "the two fixtures answer different question sets"
    shared = {k for k, v in answers.items() if answers_b[k] == v}
    # The library pin and its URL/project path are the same upstream on purpose,
    # and the four backend seams have one implemented value each — a fixture
    # answering anything else is rejected by the question's own validator.
    assert shared <= {"lib_url", "lib_ref", "lib_project", "git_backend", "secrets_backend",
                      "storage_backend", "dns_backend", "k3s_pod_cidr",
                      "k3s_service_cidr"}, (
        "answers that must differ between the fixtures now coincide: "
        + ", ".join(sorted(shared))
    )


# Answers whose fixture-A value cannot serve as evidence in a cross-render diff:
# each is an ordinary English word, a path component, or a standard example
# string, so finding it in render B says nothing about whether it was
# substituted. Each needs a targeted gate instead of the blanket scan — named
# below. Keep this list SHORT: an entry here is coverage given up.
CROSS_RENDER_EXEMPT = {
    "ci_runner_tag": (
        "'infrastructure' is also a path component (kubernetes/infrastructure/) and "
        "ordinary prose — test_every_job_carries_a_runner_tag is the targeted gate, "
        "and it takes the parameterized `cluster` fixture, so it runs on BOTH "
        "renders: a hardcoded tags: [\"infrastructure\"] fails on the unlike render"
    ),
    "external_domain": "'example.com' is RFC 2606's example domain, used in generic samples",
    "gpu": "'nvidia' is a vendor name that appears wherever the option is described",
    "lan_prefix": "'192.168.0' is a prefix of RFC1918 192.168.0.0/16, which NetworkPolicies name",
    "node_exporter_job_regex": "'node-exporter' is the upstream exporter's own name",
    "onepassword_vault": "'Homelab' appears in a vendored library script's own docstring",
}


def test_render_b_carries_no_fixture_a_values(rendered_b, answers, answers_b):
    """No answer from fixture A may appear in the non-prose files of a render
    from fixture B.

    Every hardcoded-literal defect found so far survived the shaped fixture for
    the same reason: its value happened to equal the correct one. Rendering a
    second, unlike answer set and diffing against the first is what separates
    'substituted' from 'copied'. Markdown is excluded — a doc legitimately shows
    the reference cluster's worked example, and test_no_reference_cluster_literals
    already polices identity there.
    """
    scoped = [
        (path, text) for path, text in _text_files(rendered_b) if path.suffix != ".md"
    ]
    leaks = []
    for key, value in answers.items():
        if key in CROSS_RENDER_EXEMPT:
            continue
        if not isinstance(value, str) or value == answers_b.get(key) or len(value) < 4:
            continue
        # upstream_dns_servers and friends are space-separated lists.
        for token in value.split():
            if len(token) < 4:
                continue
            for path, text in scoped:
                if token in text:
                    leaks.append(f"{path.relative_to(rendered_b)}: {key}={token}")
    assert not leaks, (
        "fixture A's answers appear in a render from fixture B — those values are "
        "hardcoded, not substituted:\n  " + "\n  ".join(sorted(set(leaks)))
    )


# --------------------------------------------------------------------------
# Kubernetes: substitution, not literals
# --------------------------------------------------------------------------


def test_cluster_config_holds_the_site_values(cluster):
    _, data = _cluster_config(cluster.path)
    assert data.get("cluster_internal_domain") == cluster.answers["internal_domain"]
    assert data.get("cluster_external_domain") == cluster.answers["external_domain"]
    assert data.get("cluster_k3s_api_vip") == cluster.answers["k3s_api_vip"]


def test_manifests_reference_substitution_placeholders(cluster):
    hits = sum(
        text.count("${cluster_internal_domain}") + text.count("${cluster_metallb_internal_vip}")
        for _, text in _k8s_files(cluster.path)
    )
    assert hits, (
        "no manifest substitutes a cluster-config key — the ConfigMap exists but "
        "nothing reads it, which means the values are hard-coded somewhere"
    )


def test_no_site_literals_in_the_kubernetes_tree(cluster):
    """The 411-domain/200-IP problem this template exists to avoid."""
    config_file, _ = _cluster_config(cluster.path)
    literals = {
        cluster.answers[key]
        for key in (
            "internal_domain",
            "external_domain",
            "k3s_api_vip",
            "metallb_public_vip",
            "metallb_internal_vip",
            "lan_cidr",
        )
    }
    offenders = []
    for path, text in _k8s_files(cluster.path):
        if path == config_file:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for value in literals:
                if value in line:
                    offenders.append(f"{path.relative_to(cluster.path)}:{lineno} {value}")
    assert not offenders, (
        "site values interpolated into manifests instead of substituted from "
        "cluster-config:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Ansible: FQCN only
# --------------------------------------------------------------------------


def test_no_vendored_roles_directory(cluster):
    assert not (cluster.path / "ansible" / "roles").exists(), (
        "the generated repo must consume weisssrv.infra from galaxy, not vendor roles"
    )


def test_requirements_pin_the_collection_at_lib_ref(cluster):
    req = cluster.path / "ansible" / "requirements.yml"
    if not req.is_file():
        pytest.skip("no ansible/requirements.yml in the render")
    # lib_ref is inherited from copier.yml's default; the tag copier resolved is
    # recorded in the render's .copier-answers.yml.
    want = yaml.safe_load((cluster.path / ".copier-answers.yml").read_text())["lib_ref"]
    doc = yaml.safe_load(req.read_text()) or {}
    entries = doc.get("collections") or []
    matches = [e for e in entries if isinstance(e, dict) and "weisssrv-lib" in str(e.get("name", ""))]
    assert matches, "requirements.yml does not install weisssrv.infra from weisssrv-lib"
    assert all(str(e.get("version")) == want for e in matches), (
        f"the collection must be pinned at lib_ref ({want})"
    )


def test_playbook_roles_are_fqcn(cluster):
    playbooks = cluster.path / "ansible" / "playbooks"
    if not playbooks.is_dir():
        pytest.skip("no ansible/playbooks in the render")
    bare = []
    for path in sorted(playbooks.rglob("*.yml")):
        try:
            plays = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for play in plays if isinstance(plays, list) else []:
            if not isinstance(play, dict):
                continue
            for entry in play.get("roles") or []:
                name = entry.get("role") if isinstance(entry, dict) else entry
                if isinstance(name, str) and name.count(".") < 2:
                    bare.append(f"{path.relative_to(cluster.path)}: {name}")
    assert not bare, "playbooks must address roles by FQCN:\n  " + "\n  ".join(bare)


# --------------------------------------------------------------------------
# CI wiring
# --------------------------------------------------------------------------


def test_generated_ci_pins_the_library(cluster):
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    # lib_ref is inherited from copier.yml's default; the tag copier resolved is
    # recorded in the render's .copier-answers.yml.
    want = yaml.safe_load((cluster.path / ".copier-answers.yml").read_text())["lib_ref"]
    includes = [inc for inc in ci.get("include", []) if isinstance(inc, dict) and "project" in inc]
    assert includes, "the generated pipeline includes no library templates"
    assert all(str(inc["ref"]) == want for inc in includes), (
        "every library include must pin lib_ref"
    )
    files = {inc["file"] for inc in includes}
    for required in ("/ci/validate/flux-lint.yml", "/ci/security/secret-detection.yml"):
        assert required in files, f"the generated pipeline is missing {required}"


def _flux_lint_include(rendered) -> dict:
    ci = _load_ci(rendered / ".gitlab-ci.yml")
    return next(
        inc for inc in ci["include"]
        if isinstance(inc, dict) and inc.get("file") == "/ci/validate/flux-lint.yml"
    )


def test_flux_lint_reads_both_configmaps(cluster):
    """The library helper takes ONE ConfigMap per call and this cluster has two
    (versions + cluster-config), so CI must go through the local flux-env.sh
    wrapper — the same entry point `task flux:lint` uses. Pointing the input at
    the vendored flux-render.sh silently drops every cluster_* substitution."""
    root = cluster.path
    inputs = _flux_lint_include(root)["inputs"]

    script = inputs["flux_render_script"]
    assert script == "scripts/flux-env.sh", (
        "flux-lint must render through scripts/flux-env.sh, not the single-file library helper"
    )
    assert (root / script).is_file()

    # The input's contract is ONE path; the second ConfigMap arrives through
    # flux-env.sh's FLUX_EXTRA_CONFIGMAPS default.
    cms = str(inputs["versions_configmap"]).split()
    assert len(cms) == 1, "versions_configmap takes a single path (see the library's spec:inputs)"
    assert (root / cms[0]).is_file(), f"flux-lint points at a missing ConfigMap: {cms[0]}"

    extra = re.search(
        r"FLUX_EXTRA_CONFIGMAPS=\"\$\{FLUX_EXTRA_CONFIGMAPS-([^}]+)\}\"",
        (root / script).read_text(),
    )
    assert extra, "flux-env.sh no longer declares a default second ConfigMap"
    assert (root / extra.group(1)).is_file(), (
        f"flux-env.sh defaults to a missing ConfigMap: {extra.group(1)}"
    )


# The cluster-invariant gates that must run over the rendered corpus, and the
# site data they read. Each covers a class kubeconform structurally cannot see:
# a chart the corpus never renders, an HPA/VPA pair on one workload, a scraped
# namespace its own NetworkPolicies do not admit Prometheus into, a PVC whose
# unset storageClassName the DefaultStorageClass plugin rewrites at create time,
# and a ClusterSecretStore readable from every namespace.
EXTRA_VALIDATION_GATES = (
    "scripts/check-hpa-vpa-invariant.py",
    "scripts/check-scrape-netpol.py",
    "scripts/check-secretstore-scope.py",
    "scripts/check-pvc-storageclass.py",
    "scripts/validate-helm-values.py",
    "scripts/autoscaling-policy.yaml",
    "scripts/helm-values-releases.yaml",
)


def test_flux_lint_runs_the_extra_validation_gates(cluster):
    """Every gate is wired AND present. A generated cluster gets the platform's
    architecture; without these it does not get the checks that keep it."""
    extra = _flux_lint_include(cluster.path)["inputs"].get("extra_validation", "")
    assert extra, "flux-lint is wired without extra_validation"
    for referenced in EXTRA_VALIDATION_GATES:
        assert referenced in extra, f"extra_validation does not run {referenced}"
        assert (cluster.path / referenced).is_file(), (
            f"extra_validation references a missing {referenced}"
        )
    # `changes:` decides whether the job runs at all, so a gate not named there
    # is skipped by exactly the MR that loosens it.
    changes = _flux_lint_include(cluster.path)["inputs"].get("changes") or []
    for referenced in EXTRA_VALIDATION_GATES:
        assert referenced in changes, (
            f"flux-lint's changes: does not name {referenced}, so an MR editing "
            "only that file never starts the job it weakens"
        )


def test_task_lint_mirrors_the_ci_lint_stage(cluster):
    """`task lint` is documented in four places as the local mirror of the CI
    lint stage. A gate wired only into CI turns that claim into a green local
    run followed by a red pipeline, which is how the four corpus gates and
    netpol-parity came to be CI-only.

    The CI side is READ, not asserted into. `extra_validation` is a free-form
    shell string, so a gate added there and nowhere else would leave a fixed
    list green while `task lint` quietly stopped mirroring the stage — the exact
    drift this gate exists to catch. EXTRA_VALIDATION_GATES stays the floor.
    """
    taskfile = yaml.safe_load((cluster.path / "Taskfile.yml").read_text()) or {}
    tasks = taskfile.get("tasks") or {}
    flux_lint = "\n".join(str(step) for step in (tasks.get("flux:lint") or {}).get("cmds") or [])
    extra = _flux_lint_include(cluster.path)["inputs"].get("extra_validation", "")
    ci_gates = set(re.findall(r"scripts/[\w.-]+\.py", extra))
    assert ci_gates, "no gate script parsed out of extra_validation — this gate examined nothing"
    floor = {gate for gate in EXTRA_VALIDATION_GATES if gate.endswith(".py")}
    for gate in sorted(ci_gates | floor):
        assert gate in flux_lint, (
            f"the CI flux-lint job runs {gate} but `task flux:lint` does not"
        )
    lint_deps = [
        str(step.get("task") if isinstance(step, dict) else step)
        for step in (tasks.get("lint") or {}).get("cmds") or []
    ]
    assert "flux:lint" in lint_deps, f"`task lint` does not run flux:lint: {lint_deps}"
    assert "lint:netpol-parity" in lint_deps, (
        "the pipeline has a netpol-parity lint job but `task lint` has no "
        f"counterpart: {lint_deps}"
    )
    netpol_task = "\n".join(
        str(step) for step in (tasks.get("lint:netpol-parity") or {}).get("cmds") or []
    )
    assert "--config scripts/netpol-except.yaml" in netpol_task, (
        "lint:netpol-parity must pass the same --config the CI job does, or the "
        "peer-less egress allowlist is empty locally and full in CI"
    )


def test_netpol_except_parity_is_gated(cluster):
    """The LAN fence has its own job: the checker reads the manifests on disk
    (so it also covers kubernetes/clusters/*/flux-system/, which no Kustomization
    builds) rather than the rendered corpus extra_validation sees."""
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    job = ci.get("netpol-parity")
    assert isinstance(job, dict), "the generated pipeline has no netpol-parity job"
    script = " ".join(str(step) for step in job.get("script") or [])
    assert "check-netpol-except-parity.py" in script
    for referenced in ("scripts/check-netpol-except-parity.py", "scripts/netpol-except.yaml"):
        assert (cluster.path / referenced).is_file(), f"netpol-parity needs a missing {referenced}"
    assert "--config scripts/netpol-except.yaml" in script, (
        "without --config the peer-less egress allowlist is empty and the shipped "
        "runner policy fails; with the wrong one the allowlist is unreviewable"
    )
    gate_needs = [
        entry.get("job") if isinstance(entry, dict) else entry
        for entry in (ci.get("validation-gate") or {}).get("needs") or []
    ]
    assert "netpol-parity" in gate_needs, (
        "validation-gate does not need netpol-parity, so a deploy proceeds past a "
        "failed LAN fence"
    )


def test_every_job_carries_a_runner_tag(cluster):
    """An untagged job lands on whichever runner accepts untagged work — for
    this cluster the shared, non-root, LAN-blocked one, which cannot install
    packages or SSH to a host. The deploy jobs are the ones that used to slip.

    Runs on BOTH renders, which is what CROSS_RENDER_EXEMPT['ci_runner_tag']
    trades the blanket leak scan away for: a `tags: ["infrastructure"]` literal
    satisfies the shaped fixture and fails the unlike one.
    """
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    tag = cluster.answers["ci_runner_tag"]
    default_tags = (ci.get("default") or {}).get("tags")
    assert default_tags and tag in default_tags, (
        "the `default:` block does not carry the configured runner tag, so a job "
        f"that names none lands on the untagged runner: {default_tags!r}"
    )
    untagged = []
    for name, job in ci.items():
        if not isinstance(job, dict) or name in {
            "include",
            "workflow",
            "variables",
            "stages",
            "default",
        }:
            continue
        parents = job.get("extends") or []
        parents = [parents] if isinstance(parents, str) else parents
        tags = job.get("tags")
        if tags is None:
            tags = next((ci[p]["tags"] for p in parents if isinstance(ci.get(p), dict) and "tags" in ci[p]), None)
        if tags is None:
            # A fragment the LIBRARY defines (.deploy-base) is not in this file;
            # `default:` above is what tags it, and is asserted separately.
            if any(p not in ci for p in parents):
                continue
            untagged.append(name)
        else:
            assert tag in tags, f"{name} carries {tags}, not the configured runner tag {tag!r}"
    assert not untagged, "jobs with no runner tag:\n  " + "\n  ".join(untagged)


# --------------------------------------------------------------------------
# Documentation points at things that exist
# --------------------------------------------------------------------------


_TASK_REF = re.compile(r"\btask ([a-z][a-z0-9]*(?::[a-z0-9-]+)+)")


_TEMPLATE_ROOT = Path(__file__).resolve().parent.parent


def _template_docs():
    """This template repo's own docs/ + README. They instruct running tasks in
    the GENERATED repo — which is why nine dead task references accumulated,
    nothing resolved them against a render."""
    for rel in ("README.md", *(p.name for p in (_TEMPLATE_ROOT / "docs").glob("*.md"))):
        path = _TEMPLATE_ROOT / ("docs/" + rel if rel != "README.md" else rel)
        if path.is_file():
            yield path, path.read_text(encoding="utf-8")


def _operator_docs(rendered: Path):
    """Every Markdown an operator follows: the generated repo's own docs, then
    this template repo's."""
    for path, text in _text_files(rendered):
        if path.suffix == ".md":
            yield path, text
    yield from _template_docs()


def _defined_tasks(root: Path) -> set[str]:
    return set((yaml.safe_load((root / "Taskfile.yml").read_text()) or {}).get("tasks") or {})


def test_documented_tasks_exist(cluster, rendered, rendered_b):
    """`task <name>` in ANY operator-facing prose must name a task a generated
    Taskfile actually defines. A renamed or dropped task otherwise leaves a
    bring-up step that silently does nothing.

    Two audiences, two strengths. The GENERATED repo's own docs are rendered
    under the same answers as its Taskfile, so every task they name must exist
    in THIS render — that is the strict half. This template repo's docs describe
    every option the template offers, including the optional-module tasks a
    modules-off cluster legitimately does not ship, so they resolve against the
    union of what the fixtures generate: still a real gate (a typo or a dropped
    task appears in neither), just not one that fails a doc for correctly
    describing a module the render turned off.
    """
    if not (cluster.path / "Taskfile.yml").is_file():
        pytest.skip("the render ships no Taskfile.yml")
    this_render = _defined_tasks(cluster.path)
    generatable = this_render | _defined_tasks(rendered) | _defined_tasks(rendered_b)

    checked = 0
    missing = []
    sources = [(path, text, this_render) for path, text in _text_files(cluster.path)
               if path.suffix == ".md"]
    sources += [(path, text, generatable) for path, text in _template_docs()]
    for path, text, defined in sources:
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in _TASK_REF.findall(line):
                if name.rstrip("*") != name:
                    continue  # a prose glob (task terraform:authentik-*)
                checked += 1
                if name not in defined:
                    missing.append(f"{path}:{lineno} task {name}")
    assert checked, "no `task <name>` reference was examined — the pattern is stale"
    assert not missing, "documentation names tasks no generated Taskfile defines:\n  " + "\n  ".join(
        missing
    )


def test_ci_doc_lists_every_validator_check():
    """docs/CI.md's check table and its `--skip` list must equal main()'s registry.

    The page is how anyone finds a check's skip name when a tool is missing from
    their machine, so a check documented nowhere is one they cannot work around,
    and a documented name that no longer exists sends them to `--skip` a check
    that silently does not skip.
    """
    import validate_render

    names = list(validate_render.CHECK_NAMES)
    text = (_TEMPLATE_ROOT / "docs" / "CI.md").read_text(encoding="utf-8")
    documented = re.findall(r"^\| `([a-z-]+)` \|", text, re.MULTILINE)
    assert documented, "no check table found in docs/CI.md — this gate examined nothing"
    assert documented == names, (
        "docs/CI.md's check table is out of step with validate_render.main():\n"
        f"  table: {documented}\n  code:  {names}"
    )
    assert ",".join(names) in text, (
        "docs/CI.md's --skip list is out of step with validate_render.CHECK_NAMES: "
        f"expected {','.join(names)}"
    )


def _section(text: str, heading: str) -> str:
    """The body of one `## <heading>` section, up to the next heading of the
    same level."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lower() == heading.lower()
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


# SETUP steps whose absence from the generated README produces a one-pass deploy
# that dies on the storage host, a `terraform plan` with no initialized backend,
# and a converged cluster nobody can sign in to.
BRINGUP_MUST_NAME = ("certs:show-host-keys", "terraform:init", "terraform:authentik-apply")


def test_generated_readme_bringup_matches_setup(cluster):
    """The generated repo's own numbered bring-up list is the doc a stranger
    reads first — it sits at the root of the tree copier just handed them and
    calls itself complete. SETUP.md is the long form of the SAME sequence, so
    the two must not describe different orders.

    Two directions, both real. Every task the README's bring-up names must be
    named by SETUP too (a task SETUP dropped or renamed cannot survive here),
    and every step in BRINGUP_MUST_NAME must appear in the README — a bring-up
    that omits one promises a single-pass `infra:deploy`, a `terraform:apply`
    with no init, or no SSO step at all.
    """
    readme = cluster.path / "README.md"
    if not readme.is_file():
        pytest.skip("the render ships no README.md")
    bringup = _section(readme.read_text(encoding="utf-8"), "Bring-up")
    assert bringup.strip(), "the generated README has no '## Bring-up' section to check"

    setup = (_TEMPLATE_ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")
    setup_tasks = set(_TASK_REF.findall(setup))
    assert setup_tasks, "no `task <name>` found in docs/SETUP.md — this gate examined nothing"

    readme_tasks = set(_TASK_REF.findall(bringup))
    assert readme_tasks, "the README's bring-up names no task — the pattern is stale"

    orphaned = sorted(readme_tasks - setup_tasks)
    assert not orphaned, (
        "the generated README's bring-up names tasks docs/SETUP.md does not — the "
        "two descriptions of the same sequence have drifted:\n  " + "\n  ".join(orphaned)
    )
    missing = sorted(name for name in BRINGUP_MUST_NAME if name not in readme_tasks)
    assert not missing, (
        "the generated README's bring-up omits steps SETUP.md documents as "
        "required on a fresh cluster:\n  " + "\n  ".join(missing)
    )
    # Keep the required list honest: each entry must still be a SETUP step.
    absent_from_setup = sorted(name for name in BRINGUP_MUST_NAME if name not in setup_tasks)
    assert not absent_from_setup, (
        "BRINGUP_MUST_NAME lists tasks docs/SETUP.md no longer names, so this gate "
        "is enforcing a sequence the long form abandoned: " + ", ".join(absent_from_setup)
    )


_TF_VAR = re.compile(r"\bTF_VAR_[A-Za-z0-9_]+")


def test_docs_never_tell_the_operator_to_add_a_tf_var_that_ships(rendered, rendered_b):
    """A doc that says "add `TF_VAR_x` to the env: block" must be describing a
    variable the generated Taskfile does NOT already set.

    It said exactly that about `TF_VAR_oauth2_client_secret_grafana` while its
    sibling commit was adding that line to the shared `terraform:authentik-*`
    env anchor. Following the instruction literally produces a duplicate key in
    one YAML mapping: go-task tolerates it (last wins) so nothing looks wrong,
    while yamllint with the repo's own config fails — `task lint` and the CI
    lint stage both go red for a reader who did what the step said.
    """
    shipped = set()
    for root in (rendered, rendered_b):
        shipped |= set(_TF_VAR.findall((root / "Taskfile.yml").read_text(encoding="utf-8")))
    assert shipped, "no TF_VAR_* found in either rendered Taskfile — this gate is stale"

    offenders = []
    for path, text in _template_docs():
        for lineno, line in enumerate(text.splitlines(), 1):
            if not re.search(r"\badd(?:ing|s)?\b", line, re.IGNORECASE):
                continue
            for name in _TF_VAR.findall(line):
                if name in shipped:
                    offenders.append(f"{path}:{lineno} {name}")
    assert not offenders, (
        "operator docs instruct adding a TF_VAR the generated Taskfile already "
        "sets — following them duplicates a YAML key and fails lint:\n  "
        + "\n  ".join(offenders)
    )


# Generated file -> the script that generates it. Both are OUTPUTS committed
# next to their source, and nothing else in the repository ever compares the two
# again: flux-lint substitutes FROM the ConfigMap so it passes on a stale pin,
# and the version bot reads all.yml so it reports that pin as current.
GENERATED_FILES = {
    "scripts/hosts.env": "generate-hosts-env.py",
    "kubernetes/infrastructure/sources/versions-configmap.yaml": "generate-versions-configmap.py",
}


def test_generated_files_are_drift_gated(cluster):
    """SETUP.md and ARCHITECTURE.md both promise that the two generated files
    are drift-gated. This is the assertion that the promise is backed by a job
    and a task, in the render, rather than by prose.

    A claimed gate is worse than an absent one: it is the reason nobody
    re-checks by hand.
    """
    root = cluster.path
    ci_text = (root / ".gitlab-ci.yml").read_text(encoding="utf-8")
    ci = _load_ci(root / ".gitlab-ci.yml")
    taskfile = yaml.safe_load((root / "Taskfile.yml").read_text()) or {}
    tasks = taskfile.get("tasks") or {}

    def script_text(job: dict) -> str:
        parts = []
        for key in ("before_script", "script", "after_script", "cmds"):
            value = job.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(str(v) for v in value)
        return "\n".join(parts)

    for target, generator in GENERATED_FILES.items():
        assert (root / target).is_file(), f"{target} is not in the render"
        assert (root / "scripts" / generator).is_file(), f"scripts/{generator} is not in the render"

        gating_jobs = [
            name
            for name, job in ci.items()
            if isinstance(job, dict)
            and generator in script_text(job)
            and "diff" in script_text(job)
            # the version-bump bot REGENERATES as part of its own MR, which is
            # the opposite of comparing — it does not count as this gate.
            and target in script_text(job)
        ]
        assert gating_jobs, (
            f"no job in the generated pipeline regenerates {target} with "
            f"scripts/{generator} and diffs the result — the drift gate "
            "docs/SETUP.md and docs/ARCHITECTURE.md promise does not exist. "
            f"(jobs naming the generator at all: "
            f"{[n for n, j in ci.items() if isinstance(j, dict) and generator in script_text(j)]})"
        )
        assert generator in ci_text and target in ci_text

    # And the same gate locally, wired into `task lint` — otherwise the first
    # time anyone learns about the drift is in CI.
    assert "lint:repo-sync" in tasks, "the generated Taskfile defines no lint:repo-sync"
    repo_sync = script_text(tasks["lint:repo-sync"])
    for target, generator in GENERATED_FILES.items():
        assert generator in repo_sync and "diff" in repo_sync, (
            f"lint:repo-sync does not regenerate-and-diff {target}"
        )
    lint_deps = [
        str(step.get("task") if isinstance(step, dict) else step)
        for step in (tasks.get("lint") or {}).get("cmds") or []
    ]
    assert "lint:repo-sync" in lint_deps, (
        "lint:repo-sync exists but `task lint` does not run it, so the local half "
        f"of the gate is opt-in: {lint_deps}"
    )


# Components the README's answer table names, and the evidence a render must
# carry for the claim to be true. The table is a contract an operator picks
# answers from, so an over-claimed answer must fail here.
README_TABLE_CLAIMS = {
    "DCGM": "dcgm",
}


def test_readme_answer_table_only_claims_what_ships(rendered, rendered_b):
    readme = (_TEMPLATE_ROOT / "README.md").read_text(encoding="utf-8")
    table = [line for line in readme.splitlines() if line.startswith("| `")]
    assert table, "no answer table found in README.md — this gate examined nothing"

    def ships(needle: str) -> bool:
        for root in (rendered, rendered_b):
            for path, text in _k8s_files(root):
                if path.name.endswith(".md"):
                    continue
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue  # prose in a comment is not a shipped component
                    if needle in stripped.lower():
                        return True
        return False

    offenders = [
        f"{claim}: named in the README answer table, absent from both renders"
        for claim, needle in README_TABLE_CLAIMS.items()
        if any(claim in line for line in table) and not ships(needle)
    ]
    assert not offenders, (
        "the README's answer table advertises components no render contains:\n  "
        + "\n  ".join(offenders)
    )


def test_runnable_quickstarts_pin_no_template_tag():
    """A quickstart block is presented as runnable, and an unpinned VCS source
    already resolves to the latest release tag — so a literal `--vcs-ref vX.Y.Z`
    buys nothing and goes stale on the next release, leaving a copy-pasteable
    command that pins a superseded template. The docs use a `<template-tag>`
    placeholder instead.
    """
    for path, text in _template_docs():
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"--vcs-ref\s+(\S+)", line):
                ref = m.group(1)
                assert not re.fullmatch(r"v\d+\.\d+\.\d+", ref), (
                    f"{path}:{lineno} pins --vcs-ref {ref}, a literal template "
                    "release that this MR will outlive; use a `<template-tag>` "
                    "placeholder, or omit the flag for the latest release."
                )


def test_relative_markdown_links_resolve(cluster):
    """Every relative `](path)` in the generated tree must resolve. The repo's
    own `task lint:doc-links` only scans docs/ and the top-level README, so the
    per-directory READMEs are covered here instead."""
    link = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
    skip = ("http://", "https://", "mailto:", "tel:", "#", "//")
    dangling = []
    for path, text in _text_files(cluster.path):
        if path.suffix != ".md":
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for target in link.findall(line):
                if target.startswith(skip):
                    continue
                resolved = target.split("#")[0]
                if resolved and not (path.parent / resolved).exists():
                    dangling.append(f"{path.relative_to(cluster.path)}:{lineno} -> {target}")
    assert not dangling, "dangling relative links in the render:\n  " + "\n  ".join(dangling)


# --------------------------------------------------------------------------
# The generated repository's own gate
# --------------------------------------------------------------------------


def test_generated_repo_passes_its_own_invariants(cluster):
    tests_dir = cluster.path / "tests"
    if not tests_dir.is_dir():
        pytest.skip("the render ships no tests/")
    env = dict(os.environ)
    # Do not let this run's cache/rootdir config leak into the nested run.
    env.pop("PYTEST_CURRENT_TEST", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests_dir)],
        cwd=cluster.path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, (
        "the generated repository fails its own invariants:\n" + result.stdout + result.stderr
    )


_PLAYBOOK_REF = re.compile(r"\b(?:ansible/)?(playbooks/[a-z0-9_/-]+\.yml)\b")


def test_documented_playbooks_exist(cluster):
    """Same contract as the task gate, for playbook paths: a doc naming
    playbooks/<x>.yml must name one the render ships (this is how a doc came
    to reference a bootstrap playbook that was never ported)."""
    missing = []
    for path, text in _operator_docs(cluster.path):
        for lineno, line in enumerate(text.splitlines(), 1):
            for rel in _PLAYBOOK_REF.findall(line):
                if not (cluster.path / "ansible" / rel).is_file():
                    missing.append(f"{path}:{lineno} {rel}")
    assert not missing, "documentation names playbooks the render does not ship:\n  " + "\n  ".join(
        missing
    )


_OP_REF = re.compile(r"op://([^/\s\"']+)/([^/\s\"']+(?: [^/\s\"']+)*)/([^\s\"'`)]+)")

# Both kinds External Secrets Operator offers. A ClusterExternalSecret wraps the
# same spec one level down, under spec.externalSecretSpec.
_ES_KINDS = ("ExternalSecret", "ClusterExternalSecret")


def _remote_keys(doc_yaml: dict) -> list[str]:
    """Every 1Password item title an ExternalSecret-family document names.

    Both shapes count: `data[].remoteRef.key` names one field of one item, and
    `dataFrom[].extract.key` pulls an item WHOLE — an item reachable only
    through dataFrom is exactly as required as any other, and reading only
    data[] made it invisible to the inventory.
    """
    spec = doc_yaml.get("spec") or {}
    # ClusterExternalSecret nests the ExternalSecret spec it templates out.
    spec = spec.get("externalSecretSpec") or spec
    keys = [(entry.get("remoteRef") or {}).get("key") for entry in (spec.get("data") or [])]
    for entry in spec.get("dataFrom") or []:
        for shape in ("extract", "find"):
            keys.append((entry.get(shape) or {}).get("key"))
    return [k for k in keys if k]


def test_credential_inventory_is_complete(cluster):
    """Every 1Password item the render actually reads — host-side `op://`
    references and in-cluster ExternalSecret/ClusterExternalSecret remoteRefs —
    must be named in PRE-SETUP.md. Without this the operator learns an item is
    required by watching a deploy fail on it."""
    pre_setup = _TEMPLATE_ROOT / "docs" / "PRE-SETUP.md"
    if not pre_setup.is_file():
        pytest.skip("no PRE-SETUP.md")
    doc = pre_setup.read_text(encoding="utf-8")

    op_items: set[str] = set()
    for _, text in _text_files(cluster.path):
        for _vault, item, _field in _OP_REF.findall(text):
            op_items.add(item)
    cluster_items: set[str] = set()
    for _path, raw in _k8s_files(cluster.path):
        for doc_yaml in yaml.safe_load_all(raw):
            if isinstance(doc_yaml, dict) and doc_yaml.get("kind") in _ES_KINDS:
                cluster_items.update(_remote_keys(doc_yaml))

    # A gate that collects nothing passes forever. Both halves must find
    # something: the k8s half is the one that silently read zero items when it
    # filtered on kind == 'ExternalSecret' alone.
    assert op_items, "no op:// references found in the render — the host-side scan is stale"
    assert cluster_items, (
        "no ExternalSecret/ClusterExternalSecret remoteRef keys found — the "
        "in-cluster scan is stale (kind filter or spec shape changed)"
    )

    # Drop matches harvested from source that itself parses op:// refs — a
    # regex fragment is not an item title. Parentheses are NOT listed: they are
    # legal in an item title, and excluding them would silently drop a real one.
    required = {
        i for i in op_items | cluster_items if not re.search(r"[\[\]^\\*+?{}|]", i)
    }
    undocumented = sorted(i for i in required if i not in doc)
    assert not undocumented, (
        "1Password items the render requires but PRE-SETUP.md never names:\n  "
        + "\n  ".join(undocumented)
    )


# Binaries whose PRE-SETUP entry is the package that provides them.
_TOOL_PACKAGES = {"ansible-playbook": "ansible"}


def test_task_preconditions_name_tools_pre_setup_installs(cluster):
    """Every binary a generated task hard-requires must be in the workstation
    tooling list.

    A failed go-task precondition ABORTS the task, so a tool the list omits is
    the stranger's first `task lint` stopping on a package nothing told them to
    install — which is how `helm` arrived with `validate-helm-values.py`.
    """
    pre_setup = _TEMPLATE_ROOT / "docs" / "PRE-SETUP.md"
    if not pre_setup.is_file():
        pytest.skip("no PRE-SETUP.md")
    doc = pre_setup.read_text(encoding="utf-8")
    required = set(
        re.findall(r"command -v ([\w.+-]+)", (cluster.path / "Taskfile.yml").read_text())
    )
    assert required, "no `command -v` precondition found — this gate examined nothing"
    undocumented = sorted(tool for tool in required if _TOOL_PACKAGES.get(tool, tool) not in doc)
    assert not undocumented, (
        "tools a generated task requires but docs/PRE-SETUP.md § 9 never names:\n  "
        + "\n  ".join(undocumented)
    )


def _controller_releases_with_secrets(root: Path):
    """(dir, HelmRelease doc) for every controller whose directory also ships an
    externalsecret.yaml, plus the total number of controller HelmReleases seen."""
    controllers = root / "kubernetes" / "infrastructure" / "controllers"
    pairs, total = [], 0
    for path in sorted(controllers.rglob("*.yaml")) if controllers.is_dir() else []:
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "HelmRelease":
                continue
            total += 1
            if (path.parent / "externalsecret.yaml").is_file():
                pairs.append((path, doc))
    return pairs, total


def test_controllers_waiting_on_a_secret_disable_helm_wait(rendered, rendered_b):
    """A controller HelmRelease with a sibling ExternalSecret must set
    `install.disableWait: true`.

    Ordering, not preference. The controllers stage is reconciled BEFORE
    anything downstream of it, and External Secrets Operator is itself installed
    in that stage — so on the very first bootstrap the Secret an ExternalSecret
    in the same directory will eventually produce does not exist yet. Helm's
    default `--wait` blocks on the pod that mounts it, the HelmRelease never
    reports Ready, and every Kustomization that `dependsOn` the controllers
    stage waits behind it: one chart deadlocks the entire first reconcile. With
    disableWait the release goes Ready, ESO delivers the Secret, and the pod
    starts on its own. Apps and observability reconcile after ESO is already
    serving, which is why the rule is scoped to this stage.
    """
    offenders, examined, seen_controllers = [], 0, 0
    for label, root in (("shaped", rendered), ("unlike", rendered_b)):
        pairs, total = _controller_releases_with_secrets(root)
        seen_controllers += total
        for path, doc in pairs:
            examined += 1
            install = (doc.get("spec") or {}).get("install") or {}
            if install.get("disableWait") is not True:
                offenders.append(
                    f"[{label}] {path.relative_to(root)}: "
                    f"{(doc.get('metadata') or {}).get('name')} has a sibling "
                    "externalsecret.yaml but no install.disableWait: true"
                )
    # Two counts, because either going to zero silently disarms this:
    # `seen_controllers` catches the stage being moved or renamed, `examined`
    # catches the pairing itself never matching in EITHER render.
    assert seen_controllers, (
        "no HelmRelease found under kubernetes/infrastructure/controllers/ in "
        "either render — the stage moved and this gate is examining nothing"
    )
    assert examined, (
        "no controller HelmRelease has a sibling externalsecret.yaml in either "
        "render — the pairing this gate is built on no longer occurs, so it can "
        "never fail; re-check the rule before deleting it"
    )
    assert not offenders, (
        "controller releases that will deadlock the first bootstrap waiting on a "
        "Secret that cannot exist yet:\n  " + "\n  ".join(offenders)
    )


def test_version_registry_covers_every_pin(cluster):
    """`scripts/check-versions.py --check-coverage` must pass in the render.

    The version-bump-bot only reports pins that have a registry entry. A pin
    with no entry is not reported as up to date — it is not reported at all, so
    the weekly job stays green while never once looking at it. That is the
    failure mode this gate exists for, and it is offline: the checker compares
    the key set of all.yml against the registry and makes no network call.

    Both fixtures matter: the registry is rendered under the SAME conditions as
    the pins, so the GPU / Tailscale / GitLab entries have to appear exactly
    when their pins do and vanish exactly when they do not.
    """
    checker = cluster.path / "scripts" / "check-versions.py"
    assert checker.is_file(), "the render ships no scripts/check-versions.py"
    result = subprocess.run(
        [sys.executable, str(checker), "--check-coverage"],
        cwd=cluster.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "version pins in the render have no registry entry (their updates are "
        "never reported):\n" + result.stdout + result.stderr
    )
    # The success line names the count, so a registry that silently emptied
    # itself — or a --check-coverage that became a no-op — is visible here.
    assert re.search(r"All (\d+) tracked pins", result.stdout), (
        f"--check-coverage no longer reports what it checked: {result.stdout!r}"
    )
    assert int(re.search(r"All (\d+) tracked pins", result.stdout).group(1)) > 1, (
        "the registry tracks at most one pin — it examined essentially nothing"
    )


# Every place the generated repo composes a path to its own git repository. The
# repository is named cluster_name (copier.yml, git_namespace help); a mismatch
# is silent because `flux bootstrap gitlab --repository=` CREATES what it cannot
# find.
_TASK_VAR = re.compile(r"\{\{\s*\.([A-Z_][A-Z0-9_]*)\s*\}\}")


def _repository_paths(root: Path) -> dict[str, str]:
    taskfile_path = root / "Taskfile.yml"
    raw = taskfile_path.read_text()
    task_vars = {
        k: str(v) for k, v in ((yaml.safe_load(raw) or {}).get("vars") or {}).items()
    }

    def expand(value: str) -> str:
        """Resolve go-task `{{.VAR}}` references against the Taskfile's own
        vars block — the composition only exists once those are substituted, and
        comparing the unexpanded literal would compare nothing."""
        return _TASK_VAR.sub(lambda m: task_vars.get(m.group(1), m.group(0)), value)

    readme = (root / "README.md").read_text() if (root / "README.md").is_file() else ""
    _, config = _cluster_config(root)
    found: dict[str, str] = {}

    # TF_STATE_PROJECT is URL-encoded (%2F); the same path either way.
    if "TF_STATE_PROJECT" in task_vars:
        found["Taskfile TF_STATE_PROJECT"] = expand(task_vars["TF_STATE_PROJECT"]).replace(
            "%2F", "/"
        )
    # flux bootstrap takes the namespace and the repository as separate flags.
    owner = re.search(r"--owner=(\S+?)\s*\\?$", raw, re.MULTILINE)
    repo = re.search(r"--repository=(\S+?)\s*\\?$", raw, re.MULTILINE)
    if owner and repo:
        found["Taskfile flux bootstrap"] = expand(f"{owner.group(1)}/{repo.group(1)}")
    row = re.search(r"\|\s*Repository\s*\|\s*`([^`]+)`", readme)
    if row:
        found["README Repository row"] = row.group(1).split("/", 1)[1]
    base = config.get("cluster_runbook_base_url", "")
    m = re.match(r"https://[^/]+/(.+?)/-/blob/", base)
    if m:
        found["cluster-config runbook base URL"] = m.group(1)
    return found


def test_repository_paths_agree(cluster):
    """Every repository path in one render must name the SAME repository.

    A mismatch is silent: `flux bootstrap` creates the repository it is pointed
    at, `terraform init` 404s on its state, and every alert runbook link
    dead-ends.
    """
    expected = f"{cluster.answers['git_namespace']}/{cluster.answers['cluster_name']}"
    paths = _repository_paths(cluster.path)
    # Named explicitly rather than "whatever we found": a regex that stops
    # matching would otherwise silently reduce this to a no-op.
    assert set(paths) == {
        "Taskfile TF_STATE_PROJECT",
        "Taskfile flux bootstrap",
        "README Repository row",
        "cluster-config runbook base URL",
    }, f"a repository-path site is no longer being examined: found {sorted(paths)}"
    wrong = {site: value for site, value in paths.items() if value != expected}
    assert not wrong, (
        f"repository paths disagree — every one must be {expected!r}:\n  "
        + "\n  ".join(f"{site}: {value}" for site, value in sorted(wrong.items()))
    )


def test_runbook_urls_resolve(rendered):
    """Alert runbook_url annotations must point at a doc the GENERATED repo
    ships, at an anchor it actually contains — otherwise every alert links to
    a 404 exactly when someone is paging through it at 3am."""
    def _anchors(md: Path) -> set[str]:
        out = set()
        for line in md.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                slug = line.lstrip("#").strip().lower()
                slug = re.sub(r"[^\w\s-]", "", slug).replace(" ", "-")
                out.add(slug)
        return out

    broken = []
    checked = 0
    for path, raw in _k8s_files(rendered):
        # Anchor on the FILENAME, not a /docs/ path segment: the annotation
        # value is `${cluster_runbook_base_url}/RUNBOOKS.md#anchor`, so the
        # path lives inside the unexpanded ConfigMap reference and a
        # segment-anchored pattern matches nothing at all.
        for m in re.finditer(r"runbook_url:.*?([A-Za-z0-9_.-]+\.md)(#[\w-]+)?", raw):
            checked += 1
            target = rendered / "docs" / m.group(1)
            if not target.is_file():
                broken.append(f"{path.relative_to(rendered)} -> docs/{m.group(1)} (missing)")
            elif m.group(2) and m.group(2).lstrip("#") not in _anchors(target):
                broken.append(f"{path.relative_to(rendered)} -> docs/{m.group(1)}{m.group(2)} (no such heading)")
    # A gate that examines nothing passes forever; this is what caught the
    # segment-anchored regex above matching zero of 25 real annotations.
    assert checked, "no runbook_url annotations were examined — the pattern is stale"
    assert not broken, "alert runbook_url annotations do not resolve:\n  " + "\n  ".join(sorted(set(broken)))


def test_runbook_index_lists_every_section():
    """docs/RUNBOOKS.md is the index of template/docs/RUNBOOKS.md.jinja and says
    so; nothing but this holds the two in sync, and the last three sections
    added to the runbook never reached the table."""
    runbook = REPO_ROOT / "template" / "docs" / "RUNBOOKS.md.jinja"
    index = REPO_ROOT / "docs" / "RUNBOOKS.md"
    headings = [
        line[3:].strip()
        for line in runbook.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    assert headings, "the runbook has no top-level sections — this gate reads nothing"
    rows = [
        line.split("|")[1].strip()
        for line in index.read_text(encoding="utf-8").splitlines()
        if line.startswith("| ") and not line.startswith("|---")
    ]
    rows = [r for r in rows if r != "Section"]
    # A row may append a parenthetical gloss ("Upgrades (versions, …)"), but its
    # first words are the heading, because the anchors are load-bearing.
    unlisted = [h for h in headings if not any(r == h or r.startswith(h + " (") for r in rows)]
    assert not unlisted, (
        "docs/RUNBOOKS.md 'What it covers' has no row for:\n  " + "\n  ".join(unlisted)
    )
    stale = [
        r for r in rows if not any(r == h or r.startswith(h + " (") for h in headings)
    ]
    assert not stale, (
        "docs/RUNBOOKS.md names sections the runbook does not have:\n  " + "\n  ".join(stale)
    )
