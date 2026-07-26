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
UPSTREAM_REPOS = ("weisssrv-lib", "weisssrv-cluster-template", "weisssrv-project-template")
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


def test_render_produces_a_repository(rendered):
    assert (rendered / ".copier-answers.yml").is_file(), "no answers file — copier update would not work"
    assert (rendered / ".gitlab-ci.yml").is_file()


def test_answers_file_records_the_fixture(rendered, answers):
    recorded = yaml.safe_load((rendered / ".copier-answers.yml").read_text())
    assert recorded["cluster_name"] == answers["cluster_name"]
    assert recorded["lib_ref"] == answers["lib_ref"]


def test_no_unrendered_jinja_statements(rendered):
    """A `{% ... %}` block in the output means a templated file was not given
    the .jinja suffix.

    Scoped to the trees where no OTHER templating language is in play: Ansible
    playbooks embed Jinja by design, and `{{ ... }}` belongs to go-task, Grafana
    dashboards and Prometheus annotations, so neither is evidence of a leak.
    """
    scoped = [
        (path, text)
        for path, text in _text_files(rendered)
        if path.relative_to(rendered).parts[0] in ("kubernetes", "terraform")
        or path.name == ".gitlab-ci.yml"
    ]
    leftovers = [
        f"{path.relative_to(rendered)}:{lineno}"
        for path, text in scoped
        for lineno, line in enumerate(text.splitlines(), 1)
        if "{%" in line
    ]
    assert not leftovers, "unrendered Jinja survived the render:\n  " + "\n  ".join(leftovers)


# --------------------------------------------------------------------------
# No reference-cluster values
# --------------------------------------------------------------------------


def test_no_reference_cluster_literals(rendered):
    offenders = []
    for path, text in _text_files(rendered):
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(repo in line for repo in UPSTREAM_REPOS):
                continue
            for needle in FORBIDDEN:
                if needle in line:
                    offenders.append(f"{path.relative_to(rendered)}:{lineno} {needle}")
    assert not offenders, (
        "reference-cluster identity leaked into the render — parameterize it:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Kubernetes: substitution, not literals
# --------------------------------------------------------------------------


def test_cluster_config_holds_the_site_values(rendered, answers):
    _, data = _cluster_config(rendered)
    assert data.get("cluster_internal_domain") == answers["internal_domain"]
    assert data.get("cluster_external_domain") == answers["external_domain"]
    assert data.get("cluster_k3s_api_vip") == answers["k3s_api_vip"]


def test_manifests_reference_substitution_placeholders(rendered):
    hits = sum(
        text.count("${cluster_internal_domain}") + text.count("${cluster_metallb_internal_vip}")
        for _, text in _k8s_files(rendered)
    )
    assert hits, (
        "no manifest substitutes a cluster-config key — the ConfigMap exists but "
        "nothing reads it, which means the values are hard-coded somewhere"
    )


def test_no_site_literals_in_the_kubernetes_tree(rendered, answers):
    """The 411-domain/200-IP problem this template exists to avoid."""
    config_file, _ = _cluster_config(rendered)
    literals = {
        answers[key]
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
    for path, text in _k8s_files(rendered):
        if path == config_file:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for value in literals:
                if value in line:
                    offenders.append(f"{path.relative_to(rendered)}:{lineno} {value}")
    assert not offenders, (
        "site values interpolated into manifests instead of substituted from "
        "cluster-config:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Ansible: FQCN only
# --------------------------------------------------------------------------


def test_no_vendored_roles_directory(rendered):
    assert not (rendered / "ansible" / "roles").exists(), (
        "the generated repo must consume weisssrv.infra from galaxy, not vendor roles"
    )


def test_requirements_pin_the_collection_at_lib_ref(rendered, answers):
    req = rendered / "ansible" / "requirements.yml"
    if not req.is_file():
        pytest.skip("no ansible/requirements.yml in the render")
    doc = yaml.safe_load(req.read_text()) or {}
    entries = doc.get("collections") or []
    matches = [e for e in entries if isinstance(e, dict) and "weisssrv-lib" in str(e.get("name", ""))]
    assert matches, "requirements.yml does not install weisssrv.infra from weisssrv-lib"
    assert all(str(e.get("version")) == answers["lib_ref"] for e in matches), (
        f"the collection must be pinned at lib_ref ({answers['lib_ref']})"
    )


def test_playbook_roles_are_fqcn(rendered):
    playbooks = rendered / "ansible" / "playbooks"
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
                    bare.append(f"{path.relative_to(rendered)}: {name}")
    assert not bare, "playbooks must address roles by FQCN:\n  " + "\n  ".join(bare)


# --------------------------------------------------------------------------
# CI wiring
# --------------------------------------------------------------------------


def test_generated_ci_pins_the_library(rendered, answers):
    ci = _load_ci(rendered / ".gitlab-ci.yml")
    includes = [inc for inc in ci.get("include", []) if isinstance(inc, dict) and "project" in inc]
    assert includes, "the generated pipeline includes no library templates"
    assert all(str(inc["ref"]) == answers["lib_ref"] for inc in includes), (
        "every library include must pin lib_ref"
    )
    files = {inc["file"] for inc in includes}
    for required in ("/ci/validate/flux-lint.yml", "/ci/security/secret-detection.yml"):
        assert required in files, f"the generated pipeline is missing {required}"


def test_flux_lint_reads_both_configmaps(rendered):
    ci = _load_ci(rendered / ".gitlab-ci.yml")
    flux = next(
        inc for inc in ci["include"]
        if isinstance(inc, dict) and inc.get("file") == "/ci/validate/flux-lint.yml"
    )
    cms = flux["inputs"]["versions_configmap"].split()
    assert len(cms) == 2, "flux-lint must substitute from both the versions and cluster-config ConfigMaps"
    for cm in cms:
        assert (rendered / cm).is_file(), f"flux-lint points at a missing ConfigMap: {cm}"


# --------------------------------------------------------------------------
# Documentation points at things that exist
# --------------------------------------------------------------------------


_TASK_REF = re.compile(r"\btask ([a-z][a-z0-9]*(?::[a-z0-9-]+)+)")


def test_documented_tasks_exist(rendered):
    """`task <name>` in the generated prose must name a task the generated
    Taskfile actually defines. A renamed or dropped task otherwise leaves a
    bring-up step that silently does nothing."""
    taskfile = rendered / "Taskfile.yml"
    if not taskfile.is_file():
        pytest.skip("the render ships no Taskfile.yml")
    defined = set((yaml.safe_load(taskfile.read_text()) or {}).get("tasks") or {})
    missing = []
    for path, text in _text_files(rendered):
        if path.suffix != ".md":
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in _TASK_REF.findall(line):
                if name not in defined:
                    missing.append(f"{path.relative_to(rendered)}:{lineno} task {name}")
    assert not missing, "documentation names tasks the Taskfile does not define:\n  " + "\n  ".join(
        missing
    )


def test_relative_markdown_links_resolve(rendered):
    """Every relative `](path)` in the generated tree must resolve. The repo's
    own `task lint:doc-links` only scans docs/ and the top-level README, so the
    per-directory READMEs are covered here instead."""
    link = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
    skip = ("http://", "https://", "mailto:", "tel:", "#", "//")
    dangling = []
    for path, text in _text_files(rendered):
        if path.suffix != ".md":
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for target in link.findall(line):
                if target.startswith(skip):
                    continue
                resolved = target.split("#")[0]
                if resolved and not (path.parent / resolved).exists():
                    dangling.append(f"{path.relative_to(rendered)}:{lineno} -> {target}")
    assert not dangling, "dangling relative links in the render:\n  " + "\n  ".join(dangling)


# --------------------------------------------------------------------------
# The generated repository's own gate
# --------------------------------------------------------------------------


def test_generated_repo_passes_its_own_invariants(rendered):
    tests_dir = rendered / "tests"
    if not tests_dir.is_dir():
        pytest.skip("the render ships no tests/")
    env = dict(os.environ)
    # Do not let this run's cache/rootdir config leak into the nested run.
    env.pop("PYTEST_CURRENT_TEST", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests_dir)],
        cwd=rendered,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, (
        "the generated repository fails its own invariants:\n" + result.stdout + result.stderr
    )
