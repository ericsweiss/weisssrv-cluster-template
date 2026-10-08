"""Coverage for ci_yaml.py — the two tag semantics stay distinct and resolvable."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import SCRIPTS, load_script

ci_yaml = load_script("ci_yaml.py")

PIPELINE = """
.base:
  script:
    - echo base
variables:
  WEISSSRV_LIB_REF: v1.2.3
build:
  stage: build
  script:
    - !reference [.base, script]
    - echo build
  rules:
    - if: '$CI_COMMIT_BRANCH'
"""


def test_safe_load_cannot_read_the_pipeline_at_all():
    """The reason this module exists."""
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(PIPELINE)


def test_reference_keeps_its_key_path_and_resolves():
    doc = ci_yaml.parse_ci(PIPELINE)
    ref = doc["build"]["script"][0]
    assert isinstance(ref, ci_yaml.Reference)
    assert list(ref) == [".base", "script"]
    assert ref.resolve(doc) == ["echo base"]


def test_an_unresolvable_reference_yields_the_default():
    doc = ci_yaml.parse_ci("a:\n  script:\n    - !reference [.missing, script]\n")
    assert doc["a"]["script"][0].resolve(doc, []) == []


def test_script_lines_collects_an_unresolvable_reference():
    doc = ci_yaml.parse_ci("a:\n  script:\n    - !reference [.missing, script]\n")
    unresolved = []
    assert ci_yaml.script_lines(doc["a"], doc, unresolved=unresolved) == []
    assert unresolved == [[".missing", "script"]]


def test_script_lines_collects_nothing_when_every_reference_resolves():
    doc = ci_yaml.parse_ci(PIPELINE)
    unresolved = []
    ci_yaml.script_lines(doc["build"], doc, unresolved=unresolved)
    assert unresolved == []


def test_script_lines_expands_the_reference():
    doc = ci_yaml.parse_ci(PIPELINE)
    assert ci_yaml.script_lines(doc["build"], doc) == ["echo base", "echo build"]


def test_null_tag_loader_collapses_where_the_structure_loader_preserves():
    doc = ci_yaml.parse_ci(PIPELINE, loader=ci_yaml.NullTagCILoader)
    assert doc["build"]["script"][0] is None


def test_jobs_drops_templates_and_reserved_keys():
    assert set(ci_yaml.jobs(ci_yaml.parse_ci(PIPELINE))) == {"build"}


def test_a_non_mapping_document_is_an_empty_mapping_not_a_crash():
    assert ci_yaml.parse_ci("- one\n- two\n") == {}


def test_load_ci_reads_a_file(tmp_path):
    path = tmp_path / ".gitlab-ci.yml"
    path.write_text(PIPELINE, encoding="utf-8")
    assert "build" in ci_yaml.load_ci(path)


SPEC_HEADER = """
spec:
  inputs:
    stage:
      default: test
---
build:
  stage: build
  script:
    - echo hi
"""


def test_a_spec_header_document_yields_the_jobs_document():
    """GitLab's inputs syntax makes a pipeline two documents. Reading only the
    first leaves a caller with no jobs at all."""
    doc = ci_yaml.parse_ci(SPEC_HEADER)
    assert "build" in doc
    assert "inputs" not in doc


def test_a_spec_header_file_still_lists_its_jobs():
    assert list(ci_yaml.jobs(ci_yaml.parse_ci(SPEC_HEADER))) == ["build"]


TAGGED = """
job:
  a: !custom scalar
  b: !flatten [x, y]
  c: !custom {k: 1}
  s: !reference .base
"""


def test_a_non_reference_tag_keeps_its_shape():
    """A pipeline using another GitLab tag must still parse: a job body that came
    back None would drop the job from every gate that walks it."""
    job = ci_yaml.parse_ci(TAGGED)["job"]
    assert job["a"] == "scalar"
    assert job["b"] == ["x", "y"]
    assert job["c"] == {"k": 1}


def test_a_scalar_reference_falls_through_to_the_passthrough():
    assert ci_yaml.parse_ci(TAGGED)["job"]["s"] == ".base"


def test_the_null_loader_nulls_every_tag():
    job = ci_yaml.parse_ci(TAGGED, loader=ci_yaml.NullTagCILoader)["job"]
    assert job == {"a": None, "b": None, "c": None, "s": None}


def test_reference_resolve_follows_a_list_index():
    ref = ci_yaml.Reference([".base", "script", 1])
    assert ref.resolve({".base": {"script": ["a", "b"]}}) == "b"


def test_reference_resolve_returns_the_default_for_an_index_out_of_range():
    ref = ci_yaml.Reference([".base", "script", 5])
    assert ref.resolve({".base": {"script": ["a"]}}, default="none") == "none"


def test_a_missing_pyyaml_names_pyyaml_not_a_missing_ci_yaml(tmp_path: Path):
    """Importers catch ImportError to report a missing companion file, so a bare
    `import yaml` here would blame ci_yaml.py for an absent PyYAML."""
    source = (SCRIPTS / "ci_yaml.py").read_text(encoding="utf-8")
    (tmp_path / "ci_yaml.py").write_text(
        source.replace("import yaml", "import yaml_not_installed_here", 1),
        encoding="utf-8",
    )
    for companion in ("deploy-preflight.py", "ci_playbook_invocations.py"):
        shutil.copy(SCRIPTS / companion, tmp_path)
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "deploy-preflight.py")],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc.returncode == 2, f"{proc.stdout}{proc.stderr}"
    assert "PyYAML" in proc.stderr
    assert "must sit next to" not in proc.stderr
