"""copier.yml is the template's API: the answer set is replayed on every
`copier update`, so renaming or dropping a question breaks every generated
cluster. These tests pin the schema and the mechanics the template relies on.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
import yaml
from jinja2 import meta

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((REPO_ROOT / "copier.yml").read_text())
QUESTIONS = {k: v for k, v in CONFIG.items() if not k.startswith("_")}
TEMPLATE_ROOT = REPO_ROOT / CONFIG["_subdirectory"]
TEMPLATES_SUFFIX = CONFIG["_templates_suffix"]

# The schema every subtree of the template is written against.
REQUIRED_QUESTIONS = {
    "cluster_name",
    "internal_domain",
    "external_domain",
    "lan_cidr",
    "lan_prefix",
    "k3s_api_vip",
    "metallb_public_vip",
    "metallb_internal_vip",
    "k3s_pod_cidr",
    "k3s_service_cidr",
    "upstream_dns_servers",
    "admin_user",
    "admin_email",
    "alert_email",
    "timezone",
    "git_backend",
    "git_host",
    "git_namespace",
    "git_repo",
    "secrets_backend",
    "onepassword_vault",
    "dns_backend",
    "compute_node_count",
    "nas_host",
    "smtp_host",
    "node_exporter_job_regex",
    "vpn_tailscale",
    "tailnet_dns_suffix",
    "gpu",
    "lib_url",
    "lib_ref",
    "lib_project",
    "ci_runner_tag",
    "ci_cpu_selector",
    "enable_semantic_release",
}

# Site identity: a wrong-but-plausible default here generates a cluster that
# looks configured and points at someone else's network.
NO_DEFAULT = {
    "cluster_name",
    "internal_domain",
    "external_domain",
    "lan_cidr",
    "k3s_api_vip",
    "metallb_public_vip",
    "metallb_internal_vip",
    "admin_user",
    "admin_email",
    "git_namespace",
    "onepassword_vault",
}


def test_required_questions_are_declared():
    assert REQUIRED_QUESTIONS <= set(QUESTIONS), (
        f"copier.yml is missing questions: {sorted(REQUIRED_QUESTIONS - set(QUESTIONS))}"
    )


def test_template_mechanics():
    assert CONFIG["_subdirectory"] == "template"
    assert CONFIG["_templates_suffix"] == ".jinja"
    assert CONFIG["_answers_file"] == ".copier-answers.yml"
    assert CONFIG["_min_copier_version"].split(".")[0] == "9"


@pytest.mark.parametrize("name", sorted(NO_DEFAULT))
def test_site_identity_has_no_literal_default(name):
    default = QUESTIONS[name].get("default")
    if default is None or default == "":
        return
    assert "{{" in str(default) or "{%" in str(default), (
        f"{name} carries the literal default {default!r}; site identity must be "
        "asked for, or derived from another answer"
    )


@pytest.mark.parametrize("name", ["git_backend", "secrets_backend", "dns_backend"])
def test_backend_questions_are_enumerated(name):
    assert QUESTIONS[name].get("choices"), f"{name} must be a choice list, not free text"


def test_unimplemented_backend_choices_fail_at_copy_time():
    """A choice with no implementation must stop the render, not produce a repo
    that looks complete and never reconciles."""
    choices = QUESTIONS["git_backend"]["choices"]
    values = set(choices.values()) if isinstance(choices, dict) else set(choices)
    if values == {"gitlab_selfhosted"}:
        pytest.skip("no unimplemented git_backend choice is offered")
    validator = QUESTIONS["git_backend"].get("validator", "")
    assert "gitlab_selfhosted" in validator, (
        "git_backend offers a choice beyond gitlab_selfhosted but no validator rejects it"
    )


class _AnyFilter(dict):
    """Jinja's parser needs every filter/test to resolve before it will walk a
    template. The generated tree uses copier's extra filters, so stand every
    unknown name in for a no-op — only the variable NAMES matter here."""

    def get(self, key, default=None):  # noqa: D102 - dict protocol
        return super().get(key, lambda *a, **kw: "")


def _template_variables() -> dict[str, set[str]]:
    """Every Jinja variable the template subtree references, name -> use sites.

    Covers both halves of a copier template: file CONTENT with the templates
    suffix, and PATH segments, which copier renders whatever the suffix is.
    """
    env = jinja2.Environment()  # noqa: S701 - parsing only, nothing is rendered
    env.filters = _AnyFilter(env.filters)
    env.tests = _AnyFilter(env.tests)
    found: dict[str, set[str]] = {}

    def scan(text: str, label: str) -> None:
        for name in meta.find_undeclared_variables(env.parse(text)):
            found.setdefault(name, set()).add(label)

    for path in sorted(TEMPLATE_ROOT.rglob("*")):
        rel = path.relative_to(TEMPLATE_ROOT)
        for segment in rel.parts:
            if "{{" in segment or "{%" in segment:
                scan(segment, f"path {rel}")
        if path.is_file() and path.suffix == TEMPLATES_SUFFIX:
            scan(path.read_text(), str(rel))
    return found


def test_every_template_variable_is_a_declared_question():
    """A variable the template reads but copier never asks for renders as the
    `| default(...)` fallback — or, without one, as an empty string. Either way
    the operator has no way to set it and no answer is recorded for
    `copier update`."""
    undeclared = {
        name: sorted(sites)
        for name, sites in _template_variables().items()
        if not name.startswith("_") and name not in QUESTIONS
    }
    assert not undeclared, "template/ reads variables copier.yml does not declare:\n  " + "\n  ".join(
        f"{name}: {', '.join(sites)}" for name, sites in sorted(undeclared.items())
    )


def test_no_question_is_dead():
    """A declared question nothing reads is answered by every operator and
    changes nothing — either wire it up or drop it."""
    used = set(_template_variables())
    # A question can also be consumed by copier itself (another question's
    # default, validator or `when`), which is a legitimate use.
    config_text = (REPO_ROOT / "copier.yml").read_text()
    dead = sorted(
        name
        for name in QUESTIONS
        if name not in used and config_text.count(name) < 2
    )
    assert not dead, f"copier.yml declares questions nothing reads: {dead}"


def test_answer_fixture_covers_every_question():
    fixture = yaml.safe_load((REPO_ROOT / "tests" / "answers-weisssrv-shaped.yml").read_text())
    conditional = {name for name, q in QUESTIONS.items() if "when" in q}
    missing = set(QUESTIONS) - set(fixture) - conditional
    assert not missing, (
        f"tests/answers-weisssrv-shaped.yml does not answer {sorted(missing)} — "
        "the render test would silently exercise the default instead"
    )
