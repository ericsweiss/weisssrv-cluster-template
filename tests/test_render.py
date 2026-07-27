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


def test_the_two_fixtures_answer_differently(answers, answers_b):
    """The contrast fixture only proves anything while its answers differ. A
    key that drifts back into agreement silently disarms the leak check below."""
    assert set(answers) == set(answers_b), "the two fixtures answer different question sets"
    shared = {k for k, v in answers.items() if answers_b[k] == v}
    # The library pin and its URL/project path are the same upstream on purpose.
    assert shared <= {"lib_url", "lib_ref", "lib_project", "git_backend", "secrets_backend",
                      "dns_backend", "tailnet_dns_suffix", "k3s_pod_cidr", "k3s_service_cidr"}, (
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
        "ordinary prose — test_every_job_carries_a_runner_tag is the targeted gate"
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


def test_render_b_has_no_unrendered_jinja(rendered_b):
    """The `{% if %}` branches the shaped fixture never takes (both optional
    modules off) still have to render."""
    leftovers = [
        f"{path.relative_to(rendered_b)}:{lineno}"
        for path, text in _text_files(rendered_b)
        if path.relative_to(rendered_b).parts[0] in ("kubernetes", "terraform")
        or path.name == ".gitlab-ci.yml"
        for lineno, line in enumerate(text.splitlines(), 1)
        if "{%" in line
    ]
    assert not leftovers, "unrendered Jinja survived render B:\n  " + "\n  ".join(leftovers)


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


def _flux_lint_include(rendered) -> dict:
    ci = _load_ci(rendered / ".gitlab-ci.yml")
    return next(
        inc for inc in ci["include"]
        if isinstance(inc, dict) and inc.get("file") == "/ci/validate/flux-lint.yml"
    )


def test_flux_lint_reads_both_configmaps(rendered):
    """The library helper takes ONE ConfigMap per call and this cluster has two
    (versions + cluster-config), so CI must go through the local flux-env.sh
    wrapper — the same entry point `task flux:lint` uses. Pointing the input at
    the vendored flux-render.sh silently drops every cluster_* substitution."""
    flux = _flux_lint_include(rendered)
    inputs = flux["inputs"]

    script = inputs["flux_render_script"]
    assert script == "scripts/flux-env.sh", (
        "flux-lint must render through scripts/flux-env.sh, not the single-file library helper"
    )
    assert (rendered / script).is_file()

    # The input's contract is ONE path; the second ConfigMap arrives through
    # flux-env.sh's FLUX_EXTRA_CONFIGMAPS default.
    cms = str(inputs["versions_configmap"]).split()
    assert len(cms) == 1, "versions_configmap takes a single path (see the library's spec:inputs)"
    assert (rendered / cms[0]).is_file(), f"flux-lint points at a missing ConfigMap: {cms[0]}"

    extra = re.search(
        r"FLUX_EXTRA_CONFIGMAPS=\"\$\{FLUX_EXTRA_CONFIGMAPS-([^}]+)\}\"",
        (rendered / script).read_text(),
    )
    assert extra, "flux-env.sh no longer declares a default second ConfigMap"
    assert (rendered / extra.group(1)).is_file(), (
        f"flux-env.sh defaults to a missing ConfigMap: {extra.group(1)}"
    )


def test_flux_lint_runs_the_extra_validation_gates(rendered):
    """kustomize build never renders a chart and never joins a chart-native HPA
    to its VPA, so without extra_validation both classes of defect reach the
    cluster. Assert the two scripts and their config data are wired AND present."""
    extra = _flux_lint_include(rendered)["inputs"].get("extra_validation", "")
    assert extra, "flux-lint is wired without extra_validation"
    for referenced in (
        "scripts/check-hpa-vpa-invariant.py",
        "scripts/validate-helm-values.py",
        "scripts/autoscaling-policy.yaml",
        "scripts/helm-values-releases.yaml",
    ):
        assert referenced in extra, f"extra_validation does not run {referenced}"
        assert (rendered / referenced).is_file(), f"extra_validation references a missing {referenced}"


def test_every_job_carries_a_runner_tag(rendered, answers):
    """An untagged job lands on whichever runner accepts untagged work — for
    this cluster the shared, non-root, LAN-blocked one, which cannot install
    packages or SSH to a host. The deploy jobs are the ones that used to slip."""
    ci = _load_ci(rendered / ".gitlab-ci.yml")
    tag = answers["ci_runner_tag"]
    untagged = []
    for name, job in ci.items():
        if not isinstance(job, dict) or name in {"include", "workflow", "variables", "stages"}:
            continue
        parents = job.get("extends") or []
        parents = [parents] if isinstance(parents, str) else parents
        tags = job.get("tags")
        if tags is None:
            tags = next((ci[p]["tags"] for p in parents if isinstance(ci.get(p), dict) and "tags" in ci[p]), None)
        if tags is None:
            untagged.append(name)
        else:
            assert tag in tags, f"{name} carries {tags}, not the configured runner tag {tag!r}"
    assert not untagged, "jobs with no runner tag:\n  " + "\n  ".join(untagged)


# --------------------------------------------------------------------------
# Documentation points at things that exist
# --------------------------------------------------------------------------


_TASK_REF = re.compile(r"\btask ([a-z][a-z0-9]*(?::[a-z0-9-]+)+)")


_TEMPLATE_ROOT = Path(__file__).resolve().parent.parent


def _operator_docs(rendered: Path):
    """Every Markdown an operator follows: the generated repo's own docs AND
    this template repo's docs/ + README, which instruct running tasks in the
    GENERATED repo. The second set is why nine dead task references
    accumulated — nothing resolved them against a render."""
    for path, text in _text_files(rendered):
        if path.suffix == ".md":
            yield path, text
    for rel in ("README.md", *(p.name for p in (_TEMPLATE_ROOT / "docs").glob("*.md"))):
        path = _TEMPLATE_ROOT / ("docs/" + rel if rel != "README.md" else rel)
        if path.is_file():
            yield path, path.read_text(encoding="utf-8")


def test_documented_tasks_exist(rendered):
    """`task <name>` in ANY operator-facing prose must name a task the
    generated Taskfile actually defines. A renamed or dropped task otherwise
    leaves a bring-up step that silently does nothing."""
    taskfile = rendered / "Taskfile.yml"
    if not taskfile.is_file():
        pytest.skip("the render ships no Taskfile.yml")
    defined = set((yaml.safe_load(taskfile.read_text()) or {}).get("tasks") or {})
    missing = []
    for path, text in _operator_docs(rendered):
        if path.suffix != ".md":
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in _TASK_REF.findall(line):
                if name.rstrip("*") != name:
                    continue  # a prose glob (task terraform:authentik-*)
                if name not in defined:
                    missing.append(f"{path}:{lineno} task {name}")
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


_PLAYBOOK_REF = re.compile(r"\b(?:ansible/)?(playbooks/[a-z0-9_/-]+\.yml)\b")


def test_documented_playbooks_exist(rendered):
    """Same contract as the task gate, for playbook paths: a doc naming
    playbooks/<x>.yml must name one the render ships (this is how a doc came
    to reference a bootstrap playbook that was never ported)."""
    missing = []
    for path, text in _operator_docs(rendered):
        for lineno, line in enumerate(text.splitlines(), 1):
            for rel in _PLAYBOOK_REF.findall(line):
                if not (rendered / "ansible" / rel).is_file():
                    missing.append(f"{path}:{lineno} {rel}")
    assert not missing, "documentation names playbooks the render does not ship:\n  " + "\n  ".join(
        missing
    )


_OP_REF = re.compile(r"op://([^/\s\"']+)/([^/\s\"']+(?: [^/\s\"']+)*)/([^\s\"'`)]+)")


def test_credential_inventory_is_complete(rendered):
    """Every 1Password item the render actually reads — host-side `op://`
    references and in-cluster ExternalSecret remoteRefs — must be named in
    PRE-SETUP.md. Without this the operator learns an item is required by
    watching a deploy fail on it."""
    pre_setup = _TEMPLATE_ROOT / "docs" / "PRE-SETUP.md"
    if not pre_setup.is_file():
        pytest.skip("no PRE-SETUP.md")
    doc = pre_setup.read_text(encoding="utf-8")

    required: set[str] = set()
    for _, text in _text_files(rendered):
        for _vault, item, _field in _OP_REF.findall(text):
            required.add(item)
    for _path, raw in _k8s_files(rendered):
        for doc_yaml in yaml.safe_load_all(raw):
            if not isinstance(doc_yaml, dict) or doc_yaml.get("kind") != "ExternalSecret":
                continue
            for entry in (doc_yaml.get("spec", {}).get("data") or []):
                key = (entry.get("remoteRef") or {}).get("key")
                if key:
                    required.add(key)

    # Drop matches harvested from source that itself parses op:// refs — a
    # regex fragment is not an item title.
    required = {i for i in required if not re.search(r"[\[\]^\\*+?{}|()]", i)}
    undocumented = sorted(i for i in required if i not in doc)
    assert not undocumented, (
        "1Password items the render requires but PRE-SETUP.md never names:\n  "
        + "\n  ".join(undocumented)
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
    for path, raw in _k8s_files(rendered):
        for m in re.finditer(r"runbook_url:\s*\S*/docs/([A-Za-z0-9_.-]+)(#[\w-]+)?", raw):
            target = rendered / "docs" / m.group(1)
            if not target.is_file():
                broken.append(f"{path.relative_to(rendered)} -> docs/{m.group(1)} (missing)")
            elif m.group(2) and m.group(2).lstrip("#") not in _anchors(target):
                broken.append(f"{path.relative_to(rendered)} -> docs/{m.group(1)}{m.group(2)} (no such heading)")
    assert not broken, "alert runbook_url annotations do not resolve:\n  " + "\n  ".join(sorted(set(broken)))
