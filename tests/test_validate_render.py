"""Unit proofs for the gates inside tests/validate_render.py.

The validator is otherwise exercised only end-to-end, so a guard that cannot
fail would report green forever. Each test asserts both of a helper's verdicts.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

import validate_render

PIN = "v9.9.9"


# Signing is forced off: an operator with commit.gpgsign or tag.gpgsign on
# globally would otherwise have these fixtures fail on a missing key.
_GIT_CONFIG = ("-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *_GIT_CONFIG, *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def lib_checkout(tmp_path) -> Path:
    """A real git repository whose HEAD carries two tags, as a release commit
    that also got a follow-up tag does."""
    repo = tmp_path / "weisssrv-lib"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "--allow-empty", "-q", "-m", "release")
    _git(repo, "tag", PIN)
    _git(repo, "tag", "v9.9.9-also")
    return repo


def test_the_lib_ref_guard_accepts_a_checkout_at_the_pin(lib_checkout):
    assert validate_render.lib_checkout_problems(lib_checkout, PIN) == []
    assert validate_render.lib_checkout_problems(lib_checkout, "v9.9.9-also") == []


def test_the_lib_ref_guard_rejects_a_checkout_at_another_ref(lib_checkout):
    problems = validate_render.lib_checkout_problems(lib_checkout, "v0.1.0")
    assert len(problems) == 1
    assert "v0.1.0" in problems[0] and PIN in problems[0]


def test_the_lib_ref_guard_rejects_a_dirty_checkout_at_the_pin(lib_checkout):
    """A checkout on the pinned tag with uncommitted edits serves files no
    release contains, so every lib-reading gate would compare the render against
    an in-flight next release and report green."""
    (lib_checkout / "scripts" / "check-vendored-copies.py").parent.mkdir(exist_ok=True)
    (lib_checkout / "scripts" / "check-vendored-copies.py").write_text("tracked\n")
    _git(lib_checkout, "add", "scripts/check-vendored-copies.py")
    _git(lib_checkout, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", "add")
    _git(lib_checkout, "tag", "-f", PIN)
    (lib_checkout / "scripts" / "check-vendored-copies.py").write_text("edited\n")

    problems = validate_render.lib_checkout_problems(lib_checkout, PIN)
    assert len(problems) == 1
    assert "uncommitted changes" in problems[0]
    assert "scripts/check-vendored-copies.py" in problems[0]


def test_the_lib_ref_guard_reports_a_git_failure_as_unverified(tmp_path):
    """A directory git cannot answer for is not "no tag": the guard has verified
    nothing and has to say so, or the lib-reading checks run unguarded."""
    problems = validate_render.lib_checkout_problems(tmp_path, PIN)
    assert len(problems) == 1
    assert "unverified" in problems[0]


def test_the_pinned_lib_ref_is_the_copier_default():
    config = yaml.safe_load((validate_render.render_cluster.REPO_ROOT / "copier.yml").read_text())
    assert validate_render._pinned_lib_ref() == config["lib_ref"]["default"]


# --------------------------------------------------------------------------
# Terraform state-backend parity
# --------------------------------------------------------------------------

STATE_PASSWORD = "op://Homelab/Git Terraform State Token/credential"


def _tf_render(root_dir: Path, roots: dict[str, dict]) -> Path:
    """A render stub: one terraform/<root>/ per root, plus the taskfile whose
    `<root>-init` env maps the parity gate reads."""
    tasks = {}
    for root, overrides in roots.items():
        (root_dir / "terraform" / root).mkdir(parents=True)
        base = {
            "TF_HTTP_ADDRESS": f"https://git/terraform/state/{root}",
            "TF_HTTP_LOCK_ADDRESS": f"https://git/terraform/state/{root}/lock",
            "TF_HTTP_UNLOCK_ADDRESS": f"https://git/terraform/state/{root}/lock",
            "TF_HTTP_USERNAME": "terraform",
            "TF_HTTP_PASSWORD": STATE_PASSWORD,
            "TF_HTTP_LOCK_METHOD": "POST",
            "TF_HTTP_UNLOCK_METHOD": "DELETE",
        }
        tasks[f"{root}-init"] = {
            "dir": "{{.TERRAFORM_DIR}}/" + root,
            "env": {**base, **overrides},
        }
    taskfile = root_dir / "taskfiles" / "terraform.yml"
    taskfile.parent.mkdir(parents=True)
    taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": tasks}))
    return root_dir


def test_the_state_backend_parity_gate_passes_roots_in_step(tmp_path):
    render = _tf_render(tmp_path, {"cloudflare": {}, "authentik": {}, "unifi": {}})
    assert validate_render.tf_state_backend_problems(render) == []


def test_the_state_backend_parity_gate_notices_one_rotated_root(tmp_path):
    """Mutation proof: one root reaching for another credential must fail the
    compare, which is the shape a rotation that updates three roots leaves."""
    render = _tf_render(
        tmp_path,
        {
            "cloudflare": {},
            "authentik": {"TF_HTTP_PASSWORD": "op://Homelab/Stale Item/credential"},
        },
    )
    problems = validate_render.tf_state_backend_problems(render)
    assert any("TF_HTTP_PASSWORD" in problem for problem in problems), problems


def test_the_state_backend_parity_gate_notices_a_shared_state_name(tmp_path):
    """Mutation proof: a macro call passing a sibling's state name writes one
    root's state over the other's."""
    render = _tf_render(
        tmp_path,
        {
            "cloudflare": {},
            "authentik": {
                "TF_HTTP_ADDRESS": "https://git/terraform/state/cloudflare",
                "TF_HTTP_LOCK_ADDRESS": "https://git/terraform/state/cloudflare/lock",
                "TF_HTTP_UNLOCK_ADDRESS": "https://git/terraform/state/cloudflare/lock",
            },
        },
    )
    problems = validate_render.tf_state_backend_problems(render)
    assert any("TF_HTTP_ADDRESS" in problem for problem in problems), problems


def test_the_state_backend_parity_gate_notices_a_root_without_state(tmp_path):
    render = _tf_render(tmp_path, {"cloudflare": {}})
    (render / "terraform" / "unifi").mkdir()
    problems = validate_render.tf_state_backend_problems(render)
    assert any("unifi" in problem for problem in problems), problems


def test_the_state_backend_parity_gate_fails_a_render_without_the_taskfile(tmp_path):
    (tmp_path / "terraform" / "cloudflare").mkdir(parents=True)
    problems = validate_render.tf_state_backend_problems(tmp_path)
    assert any("examined nothing" in problem for problem in problems), problems


# --------------------------------------------------------------------------
# Include contract
# --------------------------------------------------------------------------


def _ci_pair(tmp_path, template: str, pipeline: str) -> tuple[Path, Path]:
    """(pipeline file, library checkout) for one include under test."""
    lib = tmp_path / "lib"
    (lib / "ci" / "lint").mkdir(parents=True)
    (lib / "ci" / "lint" / "job.yml").write_text(textwrap.dedent(template))
    path = tmp_path / ".gitlab-ci.yml"
    path.write_text(textwrap.dedent(pipeline))
    return path, lib


TEMPLATE = """\
    spec:
      inputs:
        image:
          default: python:3.13-slim
        targets:
    ---
    yaml-lint:
      stage: lint
      image: $[[ inputs.image ]]
      script:
        - yamllint $[[ inputs.targets ]]
    """

PIPELINE = """\
    stages: [lint]
    include:
      - project: eric/weisssrv-lib
        ref: v0.1.0
        file: /ci/lint/job.yml
        inputs:
          targets: "tests/"
    """


def test_the_include_contract_gate_passes_a_resolving_include(tmp_path):
    pipeline, lib = _ci_pair(tmp_path, TEMPLATE, PIPELINE)
    inspected, problems = validate_render.include_contract_problems(pipeline, lib)
    assert (inspected, problems) == (1, [])


def test_the_include_contract_gate_notices_an_undeclared_input(tmp_path):
    pipeline, lib = _ci_pair(
        tmp_path, TEMPLATE, PIPELINE.replace('targets: "tests/"', 'target: "tests/"')
    )
    _, problems = validate_render.include_contract_problems(pipeline, lib)
    assert any("declares no input 'target'" in problem for problem in problems), problems
    assert any("requires input 'targets'" in problem for problem in problems), problems


def test_the_include_contract_gate_notices_an_undeclared_stage(tmp_path):
    """GitLab refuses to create a pipeline whose job names a stage the pipeline
    does not declare, so the gate has to catch it before the ref bump."""
    pipeline, lib = _ci_pair(tmp_path, TEMPLATE, PIPELINE.replace("stages: [lint]", "stages: [test]"))
    _, problems = validate_render.include_contract_problems(pipeline, lib)
    assert any("resolves to stage 'lint'" in problem for problem in problems), problems


def test_the_include_contract_gate_notices_a_missing_template(tmp_path):
    pipeline, lib = _ci_pair(tmp_path, TEMPLATE, PIPELINE.replace("job.yml", "gone.yml"))
    inspected, problems = validate_render.include_contract_problems(pipeline, lib)
    assert inspected == 0
    assert any("not in the library checkout" in problem for problem in problems), problems


# --------------------------------------------------------------------------
# The tenant wiring example the README hands the operator
# --------------------------------------------------------------------------

_GOOD_EXAMPLE = textwrap.dedent(
    """\
    # Tenant wiring

    ## Example

    ```yaml
    ---
    apiVersion: v1
    kind: Namespace
    metadata:
      name: example-app
    ```
    """
)


def _readme(tmp_path: Path, text: str) -> Path:
    render = tmp_path / "render"
    tenants = render / "kubernetes" / "clusters" / "brinemoor" / "tenants"
    tenants.mkdir(parents=True)
    (tenants / "README.md").write_text(text)
    return render


def _problems(render: Path, values: dict[str, str] | None = None) -> list[str]:
    return validate_render._tenant_example_problems(render, "1.34.0", values or {})


def test_the_tenant_example_gate_passes_a_valid_manifest(tmp_path):
    assert _problems(_readme(tmp_path, _GOOD_EXAMPLE)) == []


def test_the_tenant_example_gate_notices_a_mis_nested_key(tmp_path):
    """The defect the gate exists for: valid YAML the API rejects."""
    broken = _GOOD_EXAMPLE.replace("metadata:\n  name: example-app", "metadata:\n  name: 7")
    assert _problems(_readme(tmp_path, broken)), "a non-string object name was accepted"


def test_the_tenant_example_gate_notices_a_missing_block(tmp_path):
    """A README whose example moved out of a yaml fence must red the gate rather
    than report a clean run over nothing."""
    problems = _problems(_readme(tmp_path, "# Tenant wiring\n\n## Example\n\nSee the app template.\n"))
    assert problems and "no yaml block" in problems[0]


def test_the_tenant_example_gate_notices_a_placeholder_with_no_key(tmp_path):
    text = _GOOD_EXAMPLE.replace("name: example-app", "name: app.${cluster_no_such_key}")
    problems = _problems(_readme(tmp_path, text))
    assert problems and "cluster_no_such_key" in problems[0]


def test_the_tenant_example_gate_fails_a_render_without_the_readme(tmp_path):
    render = tmp_path / "empty"
    render.mkdir()
    assert _problems(render), "a render with no tenants README reported clean"


# --------------------------------------------------------------------------
# The self-check arms, which read this repository's own tree
# --------------------------------------------------------------------------

_OWN_PIPELINE = """\
    variables:
      WEISSSRV_LIB_REF: v9.9.9
    include:
      - project: eric/weisssrv-lib
        ref: v9.9.9
        file: /ci/lint/job.yml
    """


def _own_tree(monkeypatch, tmp_path, pipeline: str | None, pin: str = PIN) -> Path:
    """Stand in for this repository's root, so the self-check arms can be driven
    over a tree that is missing the pieces they read."""
    root = tmp_path / "own"
    (root / "scripts").mkdir(parents=True)
    (root / "copier.yml").write_text(f"lib_ref:\n  default: {pin}\n")
    if pipeline is not None:
        (root / ".gitlab-ci.yml").write_text(textwrap.dedent(pipeline))
    monkeypatch.setattr(validate_render.render_cluster, "REPO_ROOT", root)
    return root


def _fake_lib(tmp_path, engine: bool = True) -> Path:
    lib = tmp_path / "lib"
    (lib / "scripts").mkdir(parents=True)
    if engine:
        (lib / "scripts" / "check-vendored-copies.py").write_text("")
    # The unregistered-twin arms read the offer list at the pinned ref, so a
    # library with none reports that it examined nothing.
    (lib / "scripts" / "vendorable-paths.yml").write_text(
        "vendorable:\n  - scripts/check-doc-links.py\n"
    )
    return lib


def test_the_own_ref_arm_accepts_a_pipeline_pinned_to_the_fixture_ref(monkeypatch, tmp_path):
    _own_tree(monkeypatch, tmp_path, _OWN_PIPELINE)
    assert validate_render._assert_one_lib_ref() == []


def test_the_own_ref_arm_rejects_an_include_on_another_ref(monkeypatch, tmp_path):
    _own_tree(monkeypatch, tmp_path, _OWN_PIPELINE.replace("ref: v9.9.9", "ref: v0.1.0"))
    problems = validate_render._assert_one_lib_ref()
    assert len(problems) == 1
    assert "v0.1.0" in problems[0] and PIN in problems[0]


def test_the_own_ref_arm_reports_a_tree_with_no_pipeline_of_its_own(monkeypatch, tmp_path):
    """Reading the pipeline before testing that it exists raised out of the
    validator instead of letting this arm return a verdict."""
    _own_tree(monkeypatch, tmp_path, None)
    assert validate_render._assert_one_lib_ref() == []


def test_the_registered_copy_arm_reports_a_tree_with_no_pipeline(monkeypatch, tmp_path):
    root = _own_tree(monkeypatch, tmp_path, None)
    (root / validate_render.MANIFEST_RELPATH).write_text("copies: []\n")
    problems = validate_render._check_registered_copies(_fake_lib(tmp_path))
    assert len(problems) == 1
    assert "WEISSSRV_LIB_REF" in problems[0]


def test_the_registered_copy_arm_reports_a_pipeline_without_the_pin(monkeypatch, tmp_path):
    root = _own_tree(monkeypatch, tmp_path, "include: []\n")
    (root / validate_render.MANIFEST_RELPATH).write_text("copies: []\n")
    problems = validate_render._check_registered_copies(_fake_lib(tmp_path))
    assert len(problems) == 1
    assert "WEISSSRV_LIB_REF" in problems[0]


def test_the_registered_copy_arm_reports_a_missing_manifest(monkeypatch, tmp_path):
    _own_tree(monkeypatch, tmp_path, _OWN_PIPELINE)
    problems = validate_render._check_registered_copies(_fake_lib(tmp_path))
    assert len(problems) == 1
    assert "vendored-manifest.yml" in problems[0] and "must not silently skip" in problems[0]


def test_the_registered_copy_arm_reports_a_library_without_the_engine(monkeypatch, tmp_path):
    root = _own_tree(monkeypatch, tmp_path, _OWN_PIPELINE)
    (root / validate_render.MANIFEST_RELPATH).write_text("copies: []\n")
    problems = validate_render._check_registered_copies(_fake_lib(tmp_path, engine=False))
    assert len(problems) == 1
    assert "check-vendored-copies.py" in problems[0]


_PASSING_ENGINE = """\
import sys
if "--list" in sys.argv:
    print("vendored\\tscripts/check-doc-links.py\\tscripts/check-doc-links.py")
    raise SystemExit(0)
raise SystemExit(0)
"""

_DRIFTING_ENGINE = """\
import sys
if "--list" in sys.argv:
    print("vendored\\tscripts/check-doc-links.py\\tscripts/check-doc-links.py")
    raise SystemExit(0)
print("scripts/check-doc-links.py differs from the library", file=sys.stderr)
raise SystemExit(1)
"""


def _engine_lib(tmp_path, source: str) -> Path:
    """A library checkout whose comparison engine behaves as `source` says, so
    the verdict under test comes from the engine rather than the ref check."""
    lib = _fake_lib(tmp_path)
    (lib / "scripts" / "check-vendored-copies.py").write_text(source)
    return lib


def test_the_registered_copy_arm_reports_the_drift_the_engine_found(monkeypatch, tmp_path):
    """A local edit to a byte-identical library copy must red the arm, and the
    engine's own report must reach the message."""
    root = _own_tree(monkeypatch, tmp_path, _OWN_PIPELINE)
    (root / validate_render.MANIFEST_RELPATH).write_text("vendored: []\n")
    problems = validate_render._check_registered_copies(
        _engine_lib(tmp_path, _DRIFTING_ENGINE)
    )
    assert len(problems) == 1, problems
    assert "differs from the library" in problems[0]


def test_the_registered_copy_arm_passes_when_the_engine_finds_nothing(monkeypatch, tmp_path):
    """The other direction: an engine that exits 0 over a manifest that registers
    a copy is the only shape the arm may accept."""
    root = _own_tree(monkeypatch, tmp_path, _OWN_PIPELINE)
    (root / validate_render.MANIFEST_RELPATH).write_text("vendored: []\n")
    assert validate_render._check_registered_copies(_engine_lib(tmp_path, _PASSING_ENGINE)) == []


def _fake_render(tmp_path, ref: str = PIN) -> Path:
    """The pieces check_rendered_vendored reads before it runs the engine."""
    render = tmp_path / "render"
    (render / "scripts").mkdir(parents=True)
    (render / "tests").mkdir(parents=True)
    (render / validate_render.MANIFEST_RELPATH).write_text("vendored: []\n")
    (render / validate_render.RENDER_GATE_RELPATH).write_text("")
    (render / ".gitlab-ci.yml").write_text(f"variables:\n  WEISSSRV_LIB_REF: {ref}\n")
    return render


def test_the_render_vendored_gate_reports_the_drift_the_engine_found(tmp_path):
    """The render's own copies are compared by the same engine, and its report
    has to reach the Failure rather than a bare exit code."""
    with pytest.raises(validate_render.Failure) as caught:
        validate_render.check_rendered_vendored(
            _fake_render(tmp_path), lib_path=_engine_lib(tmp_path, _DRIFTING_ENGINE)
        )
    assert "differs from the library" in str(caught.value)
    assert validate_render.MANIFEST_RELPATH in str(caught.value)


def test_the_render_vendored_gate_passes_when_the_engine_finds_nothing(tmp_path, capsys):
    validate_render.check_rendered_vendored(
        _fake_render(tmp_path), lib_path=_engine_lib(tmp_path, _PASSING_ENGINE)
    )
    assert "rendered vendored gate ok" in capsys.readouterr().out


def test_an_unoffered_library_twin_in_the_render_is_not_reported(tmp_path, capsys):
    """A gate the library has written but not released cannot be registered: the
    byte-identity engine would red it. The arm reads the offer list at the pin,
    so only an OFFERED twin counts as an unregistered copy.
    """
    render = _fake_render(tmp_path)
    lib = _engine_lib(tmp_path, _PASSING_ENGINE)
    (lib / "scripts" / "brand-new-gate.py").write_text("")
    (render / "scripts" / "brand-new-gate.py").write_text("")
    validate_render.check_rendered_vendored(render, lib_path=lib)
    assert "rendered vendored gate ok" in capsys.readouterr().out

    # The other direction: once the library OFFERS it, the copy must be registered.
    (lib / "scripts" / "vendorable-paths.yml").write_text(
        "vendorable:\n  - scripts/check-doc-links.py\n  - scripts/brand-new-gate.py\n"
    )
    with pytest.raises(validate_render.Failure, match="brand-new-gate.py"):
        validate_render.check_rendered_vendored(render, lib_path=lib)


_ORPHAN_README = """\
# scripts/

## Local helpers

| Script | Used by |
|---|---|
| `collect-state.sh` | an operator |

## Site data

| File | Read by | Notes |
|---|---|---|
| `version-registry.py` | `check-versions.py` | Which upstreams to watch |
| `kubeconform-expected-skipped.txt` | `kubeconform-skipped.py` | Kinds allowed no schema |
| `hosts-env-map.yml` | `generate-hosts-env.py` | Inventory group to variable |
"""


def _orphan_scan(tmp_path: Path, readme: str) -> list[str]:
    """The orphan arm over a scripts/ directory holding one of each shape."""
    lib_scripts = tmp_path / "lib" / "scripts"
    lib_scripts.mkdir(parents=True)
    (lib_scripts / "kubeconform-skipped.py").write_text("")
    scripts = tmp_path / "render" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "README.md").write_text(readme, encoding="utf-8")
    for name in (
        "collect-state.sh",
        "kubeconform-skipped.py",
        "kubeconform-expected-skipped.txt",
        "version-registry.py",
        "hosts-env-map.yml",
    ):
        (scripts / name).write_text("")
    return validate_render._orphaned_scripts(
        scripts,
        lib_scripts,
        local=validate_render.render_local_scripts(scripts),
        site_data=validate_render.render_site_data(scripts),
    )


def test_the_orphan_scan_accepts_site_data_the_readme_table_names(tmp_path):
    """A baseline a vendored tool reads has no library twin and is not a helper,
    so the Site data table is the only thing that can declare it."""
    assert _orphan_scan(tmp_path, _ORPHAN_README) == []


def test_the_orphan_scan_reports_an_undeclared_baseline(tmp_path):
    """Mutation case: drop the .txt row and the file reads as an orphan, which is
    how an undeclared data file reaches a generated cluster."""
    without = _ORPHAN_README.replace(
        "| `kubeconform-expected-skipped.txt` | `kubeconform-skipped.py` | Kinds allowed no schema |\n",
        "",
    )
    problems = _orphan_scan(tmp_path, without)
    assert len(problems) == 1
    assert "kubeconform-expected-skipped.txt" in problems[0]
    assert "not declared local" in problems[0]


def test_the_site_data_table_is_read_by_name_not_by_suffix(tmp_path):
    """`.yml` site data passes on its suffix alone, so only the named suffixes
    may come out of the table — a row's suffix is not what admits the file."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "README.md").write_text(_ORPHAN_README, encoding="utf-8")
    assert validate_render.render_site_data(scripts) == {
        "version-registry.py",
        "kubeconform-expected-skipped.txt",
    }


def test_the_site_data_list_needs_the_rendered_readme(tmp_path):
    """Treating a missing README as "nothing declared" would turn every data file
    into an orphan and bury the real finding."""
    with pytest.raises(validate_render.Failure, match="site-data list"):
        validate_render.render_site_data(tmp_path)


def test_the_render_vendored_gate_needs_a_library_checkout(tmp_path):
    """Skipping on a missing checkout would leave the render's copies ungated."""
    with pytest.raises(validate_render.Failure, match="--lib-path is required"):
        validate_render.check_rendered_vendored(_fake_render(tmp_path))


def test_the_vendored_verdict_is_never_ok_on_an_unverified_pin():
    """The engine falls back to the library working tree when the pinned ref does
    not resolve, so the row must not claim byte-identity at the pin."""
    assert " ok " in validate_render._vendored_row(True, True)
    unverified = validate_render._vendored_row(True, False)
    assert " ok" not in unverified
    assert "ref unverified" in unverified


# --------------------------------------------------------------------------
# The substitution values the cluster gates render with
# --------------------------------------------------------------------------


def test_every_kubeconform_corpus_runs_the_same_argument_list(monkeypatch):
    """Three corpora are validated: the built sources, the tenant wiring example
    and the skipped-kind audit. A flag added to one arm alone would validate the
    other two more loosely, so all three go through one helper."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        validate_render,
        "_run",
        lambda cmd, **_kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    validate_render._kubeconform(Path("/tmp"), "1.33.0", "body", "-strict")
    argv = calls[0]
    assert argv[:4] == ["kubeconform", "-ignore-missing-schemas", "-kubernetes-version", "1.33.0"]
    assert argv.count("-schema-location") == 2
    assert argv[-2:] == ["-strict", "-"]
    for fn in (
        validate_render.check_flux,
        validate_render._tenant_example_problems,
        validate_render._skipped_kinds,
    ):
        source = inspect.getsource(fn)
        assert "_kubeconform(" in source and '"kubeconform",' not in source, (
            f"{fn.__name__} spells a kubeconform invocation of its own"
        )


def test_the_configmap_arm_fails_a_render_whose_pipeline_drops_the_flux_lint_include(tmp_path):
    """The ConfigMap paths come from the include's inputs, so a pipeline without
    it leaves the gates no substitution source — and must not read as empty."""
    (tmp_path / ".gitlab-ci.yml").write_text("include: []\n")
    with pytest.raises(validate_render.Failure, match="no flux-lint include"):
        validate_render._configmap_paths(tmp_path)


def test_the_substitution_arm_fails_a_render_without_the_versions_configmap(tmp_path):
    """The render's own helper exports the values; a tree missing the ConfigMap
    it reads fails the gate rather than substituting empty strings."""
    (tmp_path / ".gitlab-ci.yml").write_text(
        textwrap.dedent(
            """\
            include:
              - project: eric/weisssrv-lib
                ref: v9.9.9
                file: /ci/validate/flux-lint.yml
                inputs:
                  versions_configmap: kubernetes/missing.yaml
                  flux_render_script: scripts/flux-render.sh
            """
        )
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "flux-render.sh").write_text(
        'set -euo pipefail\ntest -f "$2" || { echo "no such ConfigMap: $2" >&2; exit 1; }\n'
    )
    with pytest.raises(validate_render.Failure, match="export-versions"):
        validate_render._substitutions(tmp_path, validate_render._configmap_paths(tmp_path))


# --------------------------------------------------------------------------
# flux-lint: the render's own first gate command
# --------------------------------------------------------------------------


def _stub_flux_lint(monkeypatch, returncode: int, stdout: str = "", stderr: str = ""):
    """`task flux:lint` with a fixed verdict, and the argv it was called with."""
    calls: list[list[str]] = []
    monkeypatch.setattr(validate_render, "_need", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        validate_render,
        "_run",
        lambda cmd, **_kw: calls.append(cmd)
        or subprocess.CompletedProcess(cmd, returncode, stdout, stderr),
    )
    return calls


def test_the_flux_lint_check_runs_the_rendered_task(monkeypatch, tmp_path, capsys):
    """The subject is the operator's own command, not a reimplementation of it."""
    calls = _stub_flux_lint(monkeypatch, 0)
    validate_render.check_flux_lint(tmp_path)
    assert calls == [["task", "flux:lint"]]
    assert "flux:lint ok" in capsys.readouterr().out


def test_the_flux_lint_check_fails_on_a_red_task(monkeypatch, tmp_path):
    """Mutation case: the VPA-cap arm that reds a fresh render's first command."""
    _stub_flux_lint(
        monkeypatch,
        1,
        "ERROR [authentik]: a VPA caps memory at or above the chart-rendered limit\n",
    )
    with pytest.raises(validate_render.Failure) as failure:
        validate_render.check_flux_lint(tmp_path)
    assert "exited 1" in str(failure.value)
    assert "ERROR [authentik]" in str(failure.value)


def test_the_flux_lint_failure_keeps_the_whole_output(monkeypatch, tmp_path):
    """The task reports each gate where it runs, so the finding can sit a hundred
    per-release summaries above the last line; a tail would cut it off."""
    lines = [f"line {n}" for n in range(200)]
    _stub_flux_lint(monkeypatch, 1, "\n".join(lines) + "\n", "stderr line\n")
    with pytest.raises(validate_render.Failure) as failure:
        validate_render.check_flux_lint(tmp_path)
    message = str(failure.value)
    assert "line 0" in message and "line 199" in message
    assert "stderr line" in message


def test_the_flux_lint_check_needs_its_tools_rather_than_skipping(monkeypatch, tmp_path):
    """Its whole subject is the helm-rendered arm, so a missing binary is a
    failure by name: a skip would certify a check that never ran."""
    monkeypatch.setattr(validate_render.shutil, "which", lambda _tool: None)
    with pytest.raises(validate_render.Failure, match="task is not on PATH"):
        validate_render.check_flux_lint(tmp_path)


def test_the_flux_lint_check_is_registered_without_a_library_checkout():
    """It reads the render only, so --lib-path must not gate it."""
    registry = {name: needs for name, _fn, needs in validate_render.CHECKS}
    assert registry.get("flux-lint") == frozenset()


def test_the_validator_job_provisions_the_tools_flux_lint_needs():
    """`task`, `helm` and `flux` are outside scripts/ci-fetch-tools.py's set, so
    without this the check fails in CI on a missing binary, not on a render."""
    ci_file = Path(__file__).resolve().parent.parent / ".gitlab-ci.yml"
    job = validate_render.render_cluster.load_ci(ci_file)["validate-rendered-cluster"]
    before = "\n".join(str(step) for step in job["before_script"])
    tools = ("helm", "task", "flux")
    for tool in tools:
        assert f"{tool}.tar.gz" in before, (
            f"validate-rendered-cluster installs no {tool}, which the flux-lint "
            "check requires"
        )
    assert before.count("sha256sum -c -") >= len(tools), (
        "a fetched binary is installed without verifying its pinned sha256"
    )
    assert "gettext-base" in before, (
        "validate-rendered-cluster installs no envsubst, which the render's "
        "flux:lint precondition requires"
    )


# --------------------------------------------------------------------------
# main()'s accounting: a gate's verdict has to reach the exit code
# --------------------------------------------------------------------------


def _run_main(monkeypatch, render_dir: Path, checks, *argv: str) -> int:
    """main() over a render handed in, with `checks` standing in for CHECKS."""
    monkeypatch.setattr(
        sys, "argv", ["validate_render.py", "--render-dir", str(render_dir), *argv]
    )
    monkeypatch.setattr(validate_render, "CHECKS", tuple(checks))
    return validate_render.main()


def test_main_returns_zero_when_every_check_passes(monkeypatch, tmp_path, capsys):
    ran = []
    checks = [("stub", lambda render, **_kw: ran.append(render), frozenset())]
    assert _run_main(monkeypatch, tmp_path, checks) == 0
    assert ran == [tmp_path.resolve()], "main() did not hand the render to the check"
    assert "render validated" in capsys.readouterr().out


def test_main_returns_one_when_a_check_fails(monkeypatch, tmp_path, capsys):
    """Every gate reports by raising Failure, and main() is the only thing that
    turns one into an exit code. A run that collects failures and still exits 0
    is a pipeline whose validate stage is decorative."""

    def boom(render, **_kw):
        raise validate_render.Failure("boom")

    assert _run_main(monkeypatch, tmp_path, [("stub", boom, frozenset())]) == 1
    assert "[stub] boom" in capsys.readouterr().err


def test_main_returns_one_when_a_lib_check_never_ran(monkeypatch, tmp_path, capsys):
    """The silent arm: a check needing --lib-path is skipped, not failed, so
    without this accounting a run with no checkout reports success having left
    the vendored and role gates unexecuted."""
    checks = [("needs-lib", lambda render, **_kw: None, frozenset({"lib"}))]
    assert _run_main(monkeypatch, tmp_path, checks) == 1
    err = capsys.readouterr().err
    assert "needs-lib" in err and "--lib-path" in err


def test_main_reports_a_missing_copier_instead_of_raising(monkeypatch, tmp_path, capsys):
    """copier is a module, not a PATH binary, so _need() cannot see it: without
    the probe the render raises CalledProcessError past every gate."""
    monkeypatch.setattr(sys, "argv", ["validate_render.py"])
    monkeypatch.setattr(validate_render, "CHECKS", ())
    monkeypatch.setattr(
        validate_render,
        "_run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 1, "", "No module named copier"),
    )
    assert validate_render.main() == 1
    assert "copier is not installed" in capsys.readouterr().err


def test_the_ref_waiver_helper_swaps_the_verdict():
    assert validate_render._lib_verdict("ok at the pin", "unverified", True) == "  ok at the pin"
    assert validate_render._lib_verdict("ok at the pin", "unverified", False) == "  unverified"


def test_every_lib_reading_gate_honours_the_ref_waiver():
    """A gate that read the library checkout must not print a bare `ok` when the
    pinned ref did not resolve: the verdict would claim a comparison the run
    never made. One helper owns the wording, so every gate routes through it."""
    waived = []
    for name, fn, needs in validate_render.CHECKS:
        if "lib" not in needs:
            continue
        source = inspect.getsource(fn)
        assert "_lib_verdict" in source or "_vendored_row" in source, (
            f"{name} reads the library checkout but prints its verdict directly"
        )
        assert "ref_verified" in inspect.signature(fn).parameters, (
            f"{name} does not take ref_verified, so its verdict cannot honour the waiver"
        )
        waived.append(name)
    assert len(waived) > 3, f"only {waived} need --lib-path — this gate examined almost nothing"


def test_the_data_override_rejects_a_value_with_no_name(monkeypatch, capsys):
    """A malformed --data must stop the run: silently dropped, the mixed-module
    invocation would validate the all-off render a second time."""
    monkeypatch.setattr(sys, "argv", ["validate_render.py", "--data", "vpn_tailscale"])
    with pytest.raises(SystemExit) as exit_info:
        validate_render.main()
    assert exit_info.value.code == 2
    assert "--data expects NAME=VALUE" in capsys.readouterr().err


def test_the_data_override_rejects_a_supplied_render(monkeypatch, capsys, tmp_path):
    """--render-dir hands in a tree already rendered, so an override would be
    accepted and change nothing."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_render.py", "--render-dir", str(tmp_path), "--data", "gpu=none"],
    )
    with pytest.raises(SystemExit) as exit_info:
        validate_render.main()
    assert exit_info.value.code == 2
    assert "nothing to override" in capsys.readouterr().err
