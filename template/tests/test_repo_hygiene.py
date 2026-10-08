"""Repo-level invariants that only a tracked-file check can see.

.gitignore stops a file being added by accident but says nothing about a file
already tracked, so these assert the end state instead of the rule.
"""
from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
from pathlib import Path

import pytest
from conftest import require_tool

REPO = Path(__file__).resolve().parent.parent

# Saved terraform plans. A plan resolves every variable, so it holds every
# client secret and injected password, and a secret scanner cannot
# pattern-match msgpack. Committing one is a silent leak.
PLAN_GLOBS = ["tfplan", "tfplan.json", "*.tfplan", "*.tfplan.json", "plan.out"]

# One probe per level: a plan is written from either the repo root or a module
# directory, and `-out=tfplan` writes a name `*.tfplan` does not match.
PROBES = (
    "tfplan",
    "tfplan.json",
    "terraform/authentik/tfplan",
    "terraform/cloudflare/plan.out",
)


def _is_git_work_tree() -> bool:
    run = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    )
    return run.returncode == 0


needs_git = pytest.mark.skipif(
    not _is_git_work_tree(), reason="not a git work tree (an unpacked render)"
)


def _ignore_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A scratch work tree carrying the ignore files under test."""
    require_tool("git", "ignore-rule", "Install git in the job that runs this suite.")
    for relpath, content in files.items():
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def unignored_probes(tree: Path) -> list[str]:
    """Probes no .gitignore in `tree` covers. core.excludesFile is neutralised so
    a machine-wide ignore rule cannot stand in for a missing shipped one."""
    return [
        probe
        for probe in PROBES
        if subprocess.run(
            ["git", "-C", str(tree), "-c", "core.excludesFile=/dev/null",
             "check-ignore", "-q", probe],
            capture_output=True,
        ).returncode != 0
    ]


SHIPPED_IGNORES = (".gitignore", "terraform/.gitignore")


def test_no_yamllint_config_at_the_repo_root():
    """ansible-lint discovers `.yamllint` by walking up from the repo root and
    then replaces its own yaml rule with it, losing the octal/comment checks and
    fix mode. The profile lives at lint/yamllint-relaxed.yml; every caller -c's it."""
    assert not (REPO / ".yamllint").exists(), (
        "a repo-root .yamllint disables ansible-lint's yaml rule and fix mode — "
        "keep the profile at lint/yamllint-relaxed.yml and pass it with -c"
    )


def plan_offenders(paths: list[str]) -> list[str]:
    return sorted(
        path
        for path in paths
        if any(fnmatch.fnmatch(Path(path).name, pattern) for pattern in PLAN_GLOBS)
    )


def _tracked() -> list[str]:
    run = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    )
    return run.stdout.splitlines()


@needs_git
def test_no_terraform_plan_file_is_tracked():
    offenders = plan_offenders(_tracked())
    assert not offenders, (
        f"terraform plan files are tracked: {offenders}. A plan holds every "
        "resolved variable in the clear; remove it from the index and rotate any "
        "credential it contained."
    )


def test_plan_offenders_catches_every_spelling():
    """The gate is only as good as its globs, and a tracked plan is rare enough
    that the passing case proves nothing."""
    caught = plan_offenders([
        "tfplan",
        "tfplan.json",
        "terraform/authentik/prod.tfplan",
        "terraform/unifi/prod.tfplan.json",
        "terraform/cloudflare/plan.out",
        "terraform/cloudflare/main.tf",
    ])
    assert caught == [
        "terraform/authentik/prod.tfplan",
        "terraform/cloudflare/plan.out",
        "terraform/unifi/prod.tfplan.json",
        "tfplan",
        "tfplan.json",
    ]


def test_both_gitignores_cover_the_bare_plan_name(tmp_path):
    """Replays the shipped ignore files in a scratch work tree: a render is not a
    git repo, so checking REPO directly would skip where the rules are authored."""
    shipped = {}
    for relpath in SHIPPED_IGNORES:
        source = REPO / relpath
        assert source.is_file(), f"{relpath} is gone, so plan files are no longer ignored"
        shipped[relpath] = source.read_text()
    missed = unignored_probes(_ignore_tree(tmp_path, shipped))
    assert not missed, (
        f"no shipped .gitignore covers {missed}. `terraform plan -out=tfplan` then "
        "writes an unignored file holding every resolved credential."
    )


def test_a_dropped_bare_plan_rule_is_reported(tmp_path):
    """Only `*.tfplan` left in the module ignore file: the bare `-out=tfplan` name
    must be reported, or the gate passes on the shape that leaks."""
    tree = _ignore_tree(
        tmp_path,
        {".gitignore": "*.tfplan\n", "terraform/.gitignore": "*.tfplan\n"},
    )
    assert "terraform/authentik/tfplan" in unignored_probes(tree)


DOC_LINK_SETTERS = ("Taskfile.yml", "taskfiles/lint.yml", ".gitlab-ci.yml")
DOC_LINK_SCRIPT = REPO / "scripts" / "check-doc-links.py"
_ENV_NAME = re.compile(r"\bCHECK_DOC_LINKS_[A-Z_]+\b")

# Variables set ahead of the library release that reads them: inert until the
# pin moves, and each entry expires at that bump.
PRESTAGED: set[str] = set()


def doc_link_vars_the_script_ignores(setters: dict[str, str], script: str) -> set[str]:
    """CHECK_DOC_LINKS_* names a caller sets that the gate never reads."""
    named: set[str] = set()
    for text in setters.values():
        named.update(_ENV_NAME.findall(text))
    return {name for name in named if name not in script}


def test_every_doc_link_variable_the_callers_set_is_read_by_the_gate():
    """A variable the vendored script does not implement is a gate arm that is
    advertised, set, and silently does nothing."""
    setters = {
        name: (REPO / name).read_text(encoding="utf-8")
        for name in DOC_LINK_SETTERS
        if (REPO / name).is_file()
    }
    assert setters, "none of the Taskfile tree or .gitlab-ci.yml is present"
    assert DOC_LINK_SCRIPT.is_file(), "scripts/check-doc-links.py is missing"
    unread = sorted(
        doc_link_vars_the_script_ignores(setters, DOC_LINK_SCRIPT.read_text(encoding="utf-8"))
        - PRESTAGED
    )
    assert not unread, (
        f"these CHECK_DOC_LINKS_* variables are set but not implemented by the "
        f"vendored check-doc-links.py: {unread} — the arm they select does nothing"
    )


def test_prestaged_doc_link_variables_are_still_unimplemented():
    """An entry outliving the bump that ships its arm hides the next omission."""
    setters = {
        name: (REPO / name).read_text(encoding="utf-8")
        for name in DOC_LINK_SETTERS
        if (REPO / name).is_file()
    }
    assert DOC_LINK_SCRIPT.is_file(), "scripts/check-doc-links.py is missing"
    script = DOC_LINK_SCRIPT.read_text(encoding="utf-8")
    landed = sorted(name for name in PRESTAGED if name in script)
    assert not landed, (
        f"the vendored check-doc-links.py now reads these: {landed} — drop them "
        "from PRESTAGED; the set is a staging window, not configuration."
    )
    unset = sorted(
        name
        for name in PRESTAGED
        if not any(name in text for text in setters.values())
    )
    assert not unset, (
        f"no caller sets these: {unset} — drop them from PRESTAGED rather than "
        "staging a variable nothing passes."
    )


def test_a_fabricated_doc_link_variable_is_caught():
    """Mutation case: the collector, not just the shipped state."""
    assert doc_link_vars_the_script_ignores(
        {"Taskfile.yml": "CHECK_DOC_LINKS_NOPE: '1'"}, "no such name here"
    ) == {"CHECK_DOC_LINKS_NOPE"}


KUBERNETES = REPO / "kubernetes"


def yml_manifests(root: Path) -> list[str]:
    """Manifests under `root` whose suffix the `*.yaml` walks cannot see."""
    return sorted(
        str(path.relative_to(root)) for path in root.rglob("*.yml") if path.is_file()
    )


@pytest.mark.skipif(
    not KUBERNETES.is_dir(), reason="no kubernetes tree in this repository"
)
def test_no_kubernetes_manifest_uses_the_yml_suffix():
    """Kustomize applies whatever `resources:` names, so a `.yml` manifest reaches
    the cluster while every gate that walks the source tree skips it."""
    offenders = yml_manifests(KUBERNETES)
    assert not offenders, (
        f"kubernetes manifests ending .yml: {offenders} — the manifest gates read "
        ".yaml only (conftest.k8s_documents, test_namespace_psa.py, "
        "test_stage_timeouts.py, test_runbook_coverage.py, "
        "scripts/check-guest-endpoint-parity.py, "
        "scripts/check-secret-rotation-coverage.py). Rename the file, or widen "
        "conftest.k8s_documents and those two scripts together."
    )


def test_the_yml_collector_reports_an_offender(tmp_path):
    """Mutation case: the shipped tree is all .yaml, so passing proves nothing."""
    app = tmp_path / "apps" / "foo"
    app.mkdir(parents=True)
    (app / "namespace.yml").write_text("kind: Namespace\n")
    (app / "release.yaml").write_text("kind: HelmRelease\n")
    assert yml_manifests(tmp_path) == ["apps/foo/namespace.yml"]


TESTS_DIR = Path(__file__).resolve().parent
# conftest is the one module the suites share; pytest puts it on the path for them.
SHARED_SUITE_MODULES = {"conftest"}


def cross_suite_imports(directory: Path) -> list[str]:
    """`importer -> imported` for every suite that imports a sibling suite."""
    suites = {path.stem for path in directory.glob("*.py")} - SHARED_SUITE_MODULES
    found = []
    for path in sorted(directory.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            found.append(f"{path.name} does not parse: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0]
                if root in suites and root != path.stem:
                    found.append(f"{path.name} -> {root}")
    return sorted(set(found))


def test_no_suite_imports_another_suite():
    """Extracting a helper out of a test module silently breaks its importer, and
    a test module that fails to import takes every assertion in it with it."""
    assert list(TESTS_DIR.glob("test_*.py")), "no suites to inspect"
    offenders = cross_suite_imports(TESTS_DIR)
    assert not offenders, (
        "test modules importing a sibling test module: "
        + ", ".join(offenders)
        + " — move the shared helper into tests/conftest.py and import it from there."
    )


def test_the_cross_suite_collector_reports_an_importer(tmp_path):
    """Mutation case: a sibling import is reported, conftest and stdlib are not."""
    (tmp_path / "conftest.py").write_text("REPO = 1\n")
    (tmp_path / "test_a.py").write_text("import re\nfrom conftest import REPO\n")
    (tmp_path / "test_b.py").write_text("from test_a import helper\n")
    (tmp_path / "test_c.py").write_text("import test_a\n")
    assert cross_suite_imports(tmp_path) == ["test_b.py -> test_a", "test_c.py -> test_a"]
