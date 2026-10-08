"""copier.yml is the template's API: the answer set is replayed on every
`copier update`, so renaming or dropping a question breaks every generated
cluster. These tests pin the schema and the mechanics the template relies on.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import jinja2
import pytest
import render_cluster
import yaml
from conftest import copier_env, load_script
from jinja2 import meta

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((REPO_ROOT / "copier.yml").read_text())
QUESTIONS = {k: v for k, v in CONFIG.items() if not k.startswith("_")}
TEMPLATE_ROOT = REPO_ROOT / CONFIG["_subdirectory"]
TEMPLATES_SUFFIX = CONFIG["_templates_suffix"]

# `when: false` entries: shared machinery, in scope for every question below but
# never prompted and never recorded. The gates that hold an operator-facing
# question do not apply to them.
COMPUTED = {
    name
    for name, question in QUESTIONS.items()
    if isinstance(question, dict) and question.get("when") is False
}

# The schema every subtree of the template is written against.
REQUIRED_QUESTIONS = {
    "cluster_name",
    "internal_domain",
    "external_domain",
    "lan_cidr",
    "lan_prefix",
    "lan_gateway",
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
    "secrets_backend",
    "onepassword_vault",
    "storage_backend",
    "dns_backend",
    "compute_node_count",
    "nas_host",
    "smtp_host",
    "node_exporter_job_regex",
    "vpn_tailscale",
    "tailnet_dns_suffix",
    "gpu",
    "use_unifi",
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
    assert CONFIG["_envops"]["undefined"] == "jinja2.StrictUndefined", (
        "a mis-spelled or renamed answer name must fail the render; jinja's "
        "default Undefined renders it as an empty string and stays green"
    )
    floor = tuple(int(part) for part in CONFIG["_min_copier_version"].split("."))
    assert floor >= (9, 15), (
        f"_min_copier_version {CONFIG['_min_copier_version']} is below 9.15, the "
        "first copier that resolves _envops.undefined into a class; an older one "
        "passes the raw string to jinja2 and `copier copy` dies with a TypeError"
    )


def test_copier_rejects_an_undeclared_answer_name(tmp_path):
    """`_envops.undefined: jinja2.StrictUndefined`, end to end. Without it a
    misspelled name renders as the empty string, so a manifest ships a blank
    value or a `{% if %}` silently takes its false arm — both green."""
    src = render_cluster.copy_source(tmp_path)
    (src / "template" / ("probe.txt" + TEMPLATES_SUFFIX)).write_text(
        "{{ not_a_declared_answer }}\n"
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "copier", "copy", "--defaults", "--trust",
            "--data-file", str(render_cluster.ANSWERS),
            str(src), str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "an undeclared answer name rendered anyway"
    assert "not_a_declared_answer" in result.stdout + result.stderr, (
        "the render failed for some other reason:\n" + result.stdout + result.stderr
    )


@pytest.mark.parametrize("name", sorted(NO_DEFAULT))
def test_site_identity_has_no_literal_default(name):
    default = QUESTIONS[name].get("default")
    if default is None or default == "":
        return
    assert "{{" in str(default) or "{%" in str(default), (
        f"{name} carries the literal default {default!r}; site identity must be "
        "asked for, or derived from another answer"
    )


@pytest.mark.parametrize(
    "name", ["git_backend", "secrets_backend", "storage_backend", "dns_backend"]
)
def test_backend_questions_are_enumerated(name):
    assert QUESTIONS[name].get("choices"), f"{name} must be a choice list, not free text"


# The one implemented value of each backend seam. Adding an implementation means
# adding its value here in the same change as the choice and the validator branch.
IMPLEMENTED_BACKENDS = {
    "git_backend": "gitlab_selfhosted",
    "secrets_backend": "onepassword",
    "storage_backend": "zfs",
    "dns_backend": "cloudflare",
}


# Choice lists whose every value is implemented, so the seam tests below do not
# apply to them. A new enum belongs in this set or in IMPLEMENTED_BACKENDS.
FULLY_IMPLEMENTED_ENUMS = ("gpu", "license")

# Each enum's exact choice set. A choice list may name only values the render
# produces or the validator rejects by name.
ENUM_CHOICES = {
    "git_backend": {"gitlab_selfhosted", "github"},
    "secrets_backend": {"onepassword", "vault"},
    "storage_backend": {"zfs", "csi"},
    "dns_backend": {"cloudflare", "route53"},
    "gpu": {"none", "nvidia"},
    "license": {"mit", "none"},
}


def test_every_enum_is_covered_by_a_seam_test():
    """A choices-bearing question in neither set is an enum no test holds, so a
    sixth seam could ship without a validator branch or a render arm."""
    declared = {
        name
        for name, question in QUESTIONS.items()
        if isinstance(question, dict) and question.get("choices")
    }
    covered = set(IMPLEMENTED_BACKENDS) | set(FULLY_IMPLEMENTED_ENUMS)
    assert declared == covered, (
        "copier.yml enums no seam test covers: "
        f"{sorted(declared - covered)}; named but not declared: "
        f"{sorted(covered - declared)}"
    )
    assert set(ENUM_CHOICES) == covered, (
        "ENUM_CHOICES must pin every enum: missing "
        f"{sorted(covered - set(ENUM_CHOICES))}"
    )


@pytest.mark.parametrize("name", sorted(ENUM_CHOICES))
def test_enum_choice_sets_are_pinned(name):
    """`choices` is advisory under `--data`, so a new value has to be a decision
    here rather than an edit to copier.yml alone."""
    values = set((QUESTIONS[name].get("choices") or {}).values())
    assert values == ENUM_CHOICES[name], (
        f"{name} choices changed to {sorted(values)}; pin the new set here in "
        "the same change as the render arm or the validator branch"
    )
    default = QUESTIONS[name].get("default")
    assert default in values, f"{name} defaults to {default!r}, outside its choices"


@pytest.mark.parametrize("name", sorted(IMPLEMENTED_BACKENDS))
def test_unimplemented_backend_choices_fail_at_copy_time(name):
    """A backend choice with no implementation stops the render. The choice list
    is what an operator picks from; the validator is what names the files an
    implementation has to write."""
    implemented = IMPLEMENTED_BACKENDS[name]
    assert QUESTIONS[name].get("default") == implemented
    validator = QUESTIONS[name].get("validator", "")
    assert implemented in validator, (
        f"{name} has no validator rejecting anything but {implemented!r} — an "
        "unimplemented answer would render a repo that never reconciles"
    )
    rejected = _validator_message(name, **{name: "no-such-backend"})
    assert rejected, f"{name} accepted an unimplemented value"
    assert not _validator_message(name, **{name: implemented})
    # The choice list is what an operator actually picks from, so a declared but
    # unimplemented value is the one the validator has to reject.
    declared = set((QUESTIONS[name].get("choices") or {}).values()) - {implemented}
    for value in sorted(declared):
        assert _validator_message(name, **{name: value}), (
            f"{name} accepts the declared choice {value!r}, which has no implementation"
        )


@pytest.mark.parametrize("name", sorted(IMPLEMENTED_BACKENDS))
def test_seam_questions_offer_an_unimplemented_choice(name):
    """copier parses `choices` before validators, on the --data path too, so a
    seam listing only its implemented value answers every other value with a bare
    'invalid choice' and never reaches the validator naming the files to write."""
    choices = QUESTIONS[name].get("choices") or {}
    unimplemented = sorted(set(choices.values()) - {IMPLEMENTED_BACKENDS[name]})
    assert unimplemented, (
        f"{name} declares only {IMPLEMENTED_BACKENDS[name]!r}, so its validator is "
        "unreachable — add a labelled placeholder choice for a second backend"
    )
    for label, value in choices.items():
        if value in unimplemented:
            assert "not implemented" in label, (
                f"{name} choice {value!r} has no implementation but its label "
                f"{label!r} does not say so"
            )


# Files that may define a secrets macro: the partial that owns them, plus the two
# consumers, so a macro defined locally again is still caught.
SECRETS_MACRO_FILES = (
    "partials/secrets.jinja",
    "template/.gitlab-ci.yml.jinja",
    "template/Taskfile.yml.jinja",
)
# The Taskfile tree's namespace files wrap the macros too.
SECRETS_MACRO_GLOBS = ("template/taskfiles/*.yml.jinja",)
SECRETS_MACRO_DOC = "docs/ARCHITECTURE.md"


def test_every_secrets_macro_is_documented():
    """docs/ARCHITECTURE.md is the only description of the secrets seam, because
    both consumers point at it instead of explaining themselves."""
    macros: set[str] = set()
    paths = []
    for name in SECRETS_MACRO_FILES:
        path = REPO_ROOT / name
        assert path.exists(), f"{name} is gone; update SECRETS_MACRO_FILES"
        paths.append(path)
    for pattern in SECRETS_MACRO_GLOBS:
        globbed = sorted(REPO_ROOT.glob(pattern))
        assert globbed, f"{pattern} matches nothing; update SECRETS_MACRO_GLOBS"
        paths += globbed
    for path in paths:
        macros |= set(re.findall(r"{%-?\s*macro\s+(secret_\w+)", path.read_text()))
    assert macros, (
        "no secrets macro found in " + ", ".join(SECRETS_MACRO_FILES) + " — the "
        "seam moved, so this gate is checking nothing"
    )
    doc = (REPO_ROOT / SECRETS_MACRO_DOC).read_text()
    missing = sorted(macro for macro in macros if macro not in doc)
    assert not missing, (
        f"secrets macros {missing} are absent from {SECRETS_MACRO_DOC}; the seam's "
        "only description must name every macro a generated cluster emits from"
    )


class _AnyFilter(dict):
    """Stands an unknown filter or test in for a no-op so Jinja's parser walks a
    template using copier's extras. Parsing only: a rendering environment must
    reject an unknown name rather than return an empty string."""

    def get(self, key, default=None):  # noqa: D102 - dict protocol
        return super().get(key, lambda *a, **kw: "")


def test_an_unknown_filter_fails_the_rendering_environment():
    """A validator reaching for a filter copier does not provide must raise, or
    every accept-side assertion in this file passes vacuously."""
    with pytest.raises(jinja2.TemplateAssertionError):
        copier_env().from_string("{{ 'x' | no_such_filter }}").render()


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
    """Every variable template/ reads is declared in copier.yml.

    StrictUndefined turns a typo into a render error; this test names the
    offending path or file instead, and runs without a render.
    """
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


def _validator_message(name: str, **context) -> str:
    """Render a question's validator the way copier does: a non-empty result is
    the rejection message, an empty one means the answer is accepted."""
    env = copier_env()
    # The hoisted regexes are declared above every question that reads them, so
    # copier has them in scope for all of them.
    octets = _computed("ipv4_octets_re")
    shared = {
        "ipv4_octets_re": octets,
        "ipv4_re": _computed("ipv4_re", ipv4_octets_re=octets),
        "ipv4_cidr_re": _computed("ipv4_cidr_re", ipv4_octets_re=octets),
    }
    return env.from_string(QUESTIONS[name]["validator"]).render(**{**shared, **context}).strip()


# The two job labels the template SHIPS, and therefore the two the alert rules
# name. Neither is an operator choice: `node-exporter` is the kube-prometheus
# chart's DaemonSet job, `node-exporter-host` the static Proxmox/VM scrape.
SHIPPED_EXPORTER_JOBS = ("node-exporter", "node-exporter-host")


@pytest.mark.parametrize(
    "answer,rejected",
    [
        ("node-exporter|node-exporter-host", False),   # the default
        ("node-exporter|node-exporter-host|extra", False),  # extended, both kept
        ("hostwatch|hostwatch-node", True),            # replaced wholesale
        ("node-exporter", True),                       # host scrape dropped
        ("node-exporter-host", True),                  # DaemonSet dropped
        ("", True),                                    # empty widens every alert
    ],
)
def test_node_exporter_job_regex_validator_requires_the_shipped_jobs(answer, rejected):
    """node_exporter_job_regex accepts only values containing both shipped job
    labels. Dropping one renders rules that match zero series, and a rule
    matching nothing never fires."""
    message = _validator_message("node_exporter_job_regex", node_exporter_job_regex=answer)
    assert bool(message) is rejected, (
        f"node_exporter_job_regex={answer!r} was "
        f"{'accepted' if not message else 'rejected'}, expected the opposite"
    )


def test_node_exporter_default_names_every_shipped_job():
    """The default and the manifests must not drift apart: this is what makes
    the validator above enforce something real rather than an arbitrary list."""
    default = str(QUESTIONS["node_exporter_job_regex"]["default"])
    assert set(default.split("|")) == set(SHIPPED_EXPORTER_JOBS), (
        f"the default {default!r} no longer matches the shipped exporter jobs "
        f"{SHIPPED_EXPORTER_JOBS}"
    )


# Answers that need no prose: their inline `help` is self-contained and nothing
# outside the template has to be arranged first. Everything else must be named
# in an operator doc. Keep this list short.
DOC_EXEMPT = {
    "lan_prefix": "derived from lan_cidr; the prompt's default is the answer",
    "alert_email": "defaults to admin_email, which the docs cover",
    "enable_semantic_release": "a repo-workflow toggle with no external prerequisite",
    "license": "a repo-workflow choice with no external prerequisite",
    "license_holder": "the copyright line, prompted only when license is mit",
    "license_year": "the copyright line, prompted only when license is mit",
}

_DOCS = ("PRE-SETUP.md", "SETUP.md")


def test_every_question_is_named_in_an_operator_doc():
    """Every question is named in an operator doc. One named in neither is met
    for the first time at the prompt, with nothing prepared for it."""
    prose = "\n".join(
        (REPO_ROOT / "docs" / name).read_text(encoding="utf-8")
        for name in _DOCS
        if (REPO_ROOT / "docs" / name).is_file()
    )
    assert prose, "neither operator doc could be read — this gate examined nothing"
    undocumented = sorted(
        name
        for name in QUESTIONS
        if name not in DOC_EXEMPT and name not in COMPUTED and name not in prose
    )
    assert not undocumented, (
        "copier questions named in neither docs/PRE-SETUP.md nor docs/SETUP.md:\n  "
        + "\n  ".join(undocumented)
    )


FIXTURES = ("answers-weisssrv-shaped.yml", "answers-unlike.yml")

# lib_ref is deliberately left unanswered: copier.yml's default is the single
# source of the library pin, and inheriting it makes validate-rendered-cluster exercise
# exactly the released tag — so there is no second literal to keep in step.
INHERITED = {"lib_ref"}


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_answer_fixture_covers_every_question(fixture_name):
    """Every prompted question is answered in both fixtures, gated ones included.

    A `when:` expression still records an answer, so exempting the gated
    questions would let one lose its fixture value and render the default.
    """
    fixture = yaml.safe_load((REPO_ROOT / "tests" / fixture_name).read_text())
    missing = set(QUESTIONS) - COMPUTED - set(fixture) - INHERITED
    assert not missing, (
        f"tests/{fixture_name} does not answer {sorted(missing)} — "
        "the render test would silently exercise the default instead"
    )


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_no_fixture_answers_a_computed_question(fixture_name):
    """copier takes a `--data-file` value over a SKIPPED question's default, so a
    computed entry named in an answer file replaces the expression the render
    exists to prove instead of being ignored."""
    answers = yaml.safe_load((REPO_ROOT / "tests" / fixture_name).read_text())
    answered = COMPUTED & set(answers)
    assert not answered, (
        f"tests/{fixture_name} answers computed entries {sorted(answered)} — "
        "copier would use those values instead of copier.yml's expressions"
    )
    unknown = sorted(set(answers) - set(QUESTIONS))
    assert not unknown, f"tests/{fixture_name} answers questions that do not exist: {unknown}"


# Paths a question's prose names, so an operator sent to a file finds one. A
# `weisssrv-lib ` prefix marks a LIBRARY path, which this repository cannot see.
_PROSE_PATH_RE = re.compile(
    r"(?<![\w/.<])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
    r"\.(?:py|yml|yaml|md|sh|tf|json|toml|jinja))"
)
_PROSE_FIELDS = ("help", "validator", "default", "placeholder")


def _repo_path_index() -> set[str]:
    index: set[str] = set()
    for base in (TEMPLATE_ROOT, REPO_ROOT):
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(base).as_posix()
            if relative.startswith(".git/") or "__pycache__" in relative:
                continue
            index.add(relative)
            if relative.endswith(TEMPLATES_SUFFIX):
                index.add(relative[: -len(TEMPLATES_SUFFIX)])
    return index


def test_every_path_a_question_names_exists():
    """A question's prose is where an operator is told which file to read or
    edit. A renamed or moved file leaves the prose pointing at nothing, and no
    other gate reads these strings."""
    index = _repo_path_index()
    missing = []
    for name, question in QUESTIONS.items():
        if not isinstance(question, dict):
            continue
        for field in _PROSE_FIELDS:
            text = question.get(field)
            if not isinstance(text, str):
                continue
            flat = " ".join(text.split())
            for match in _PROSE_PATH_RE.finditer(flat):
                named = match.group(1)
                if "weisssrv-lib " in flat[max(0, match.start() - 20) : match.start()]:
                    continue
                if any(p == named or p.endswith("/" + named) for p in index):
                    continue
                missing.append(f"{name}.{field} names {named}")
    assert not missing, "copier.yml prose names paths that do not exist:\n  " + "\n  ".join(missing)


def test_the_two_fixtures_cover_both_values_of_every_boolean():
    """Each bool decides whether a conditionally-named file, Taskfile block or CI
    job renders. A value no fixture takes is a branch no test ever renders."""
    shaped = yaml.safe_load((REPO_ROOT / "tests" / "answers-weisssrv-shaped.yml").read_text())
    unlike = yaml.safe_load((REPO_ROOT / "tests" / "answers-unlike.yml").read_text())
    uncovered = []
    for name, question in QUESTIONS.items():
        if not isinstance(question, dict) or question.get("type") != "bool":
            continue
        default = question.get("default")
        taken = {bool(answers.get(name, default)) for answers in (shaped, unlike)}
        uncovered += [f"{name}={missing}" for missing in sorted({True, False} - taken)]
    assert not uncovered, (
        "neither answer fixture exercises: "
        + ", ".join(sorted(uncovered))
        + " — that branch of its conditional files and CI jobs is never rendered by a test"
    )


# --------------------------------------------------------------------------
# Answer ORDER — the property that decides whether a validator can ever fire
# --------------------------------------------------------------------------

# Every field copier renders in a question's own context. A name referenced here
# resolves against AnswersMap.combined, whose `user` map fills in QUESTION ORDER,
# so a reference to a question asked LATER is simply absent.
RENDERED_FIELDS = ("validator", "default", "placeholder", "when", "help")

QUESTION_ORDER = {name: i for i, name in enumerate(QUESTIONS)}


def _referenced_questions(text: str) -> set[str]:
    env = jinja2.Environment()  # noqa: S701 - parsing only, nothing is rendered
    env.filters = _AnyFilter(env.filters)
    env.tests = _AnyFilter(env.tests)
    return {n for n in meta.find_undeclared_variables(env.parse(text)) if n in QUESTIONS}


def test_no_question_references_an_answer_asked_later():
    """No validator forward-references a later answer: it is undefined
    interactively and supplied on the `--data-file` path, so it enforces
    nothing in the mode operators use."""
    violations: list[str] = []
    for name, question in QUESTIONS.items():
        if not isinstance(question, dict):
            continue
        for field in RENDERED_FIELDS:
            value = question.get(field)
            if not isinstance(value, str):
                continue
            for referenced in sorted(_referenced_questions(value)):
                if QUESTION_ORDER[referenced] > QUESTION_ORDER[name]:
                    violations.append(
                        f"{name}.{field} references {referenced}, which copier asks "
                        f"{QUESTION_ORDER[referenced] - QUESTION_ORDER[name]} question(s) later"
                    )
    assert not violations, (
        "answers referenced before they are asked — undefined interactively, "
        "populated only in --data/update mode:\n  " + "\n  ".join(violations)
    )


# --------------------------------------------------------------------------
# Address-plan collisions
# --------------------------------------------------------------------------

# The addresses hosts.yml composes for everything the operator does NOT choose.
# Keep in step with template/ansible/inventories/prod/hosts.yml.jinja.
COMPOSED_BANDS = {
    "192.168.0.11": "Proxmox host band",
    "192.168.0.19": "Proxmox host band, top",
    "192.168.0.23": "SMTP relay",
    "192.168.0.31": "k3s server band",
    "192.168.0.41": "k3s agent band",
}

VIP_ANSWERS = {
    "k3s_api_vip": "192.168.0.161",
    "metallb_public_vip": "192.168.0.100",
    "metallb_internal_vip": "192.168.0.101",
    "lan_gateway": "192.168.0.1",
}


def _vip_labels(vips: dict = VIP_ANSWERS) -> dict:
    """The address -> label map the collision validators read, rendered from
    copier.yml so the shared entry is under test too."""
    return {"vip_labels": _computed("vip_labels", **vips)}


def test_vip_labels_names_every_answered_address():
    """A VIP missing from the map is an answer the collision validators compare
    against nothing, so they accept it."""
    labels = _vip_labels()["vip_labels"]
    assert set(labels) == set(VIP_ANSWERS.values()), (
        f"vip_labels does not cover every VIP answer: {sorted(labels)}"
    )
    assert all(labels.values()), "vip_labels carries an empty label"


def test_no_validator_restates_the_vip_label_map():
    """vip_labels is the one home for the map. A validator composing its own
    drifts the moment a VIP question is added."""
    offenders = sorted(
        name
        for name, question in QUESTIONS.items()
        if isinstance(question, dict)
        and isinstance(question.get("validator"), str)
        and "the k3s API VIP" in question["validator"]
    )
    assert not offenders, (
        "validators compose their own VIP label map instead of reading "
        "vip_labels: " + ", ".join(offenders)
    )


# A band spelled inside a validator instead of read from
# reserved_address_bands: `31 <= _o <= 39`, `_o == 23`, `.41-.49`.
_BAND_LITERAL = re.compile(r"\d+\s*<=\s*_[a-z0-9_]+\s*<=\s*\d+|_o\s*==\s*\d+|\.\d+-\.\d+")


def test_no_validator_restates_a_reserved_band():
    """reserved_address_bands is the one place the address scheme is written.
    A validator spelling a band as a literal accepts an answer that collides
    with a generated guest as soon as the bands move."""
    offenders = [
        f"{name}: {match.group(0)}"
        for name, question in sorted(QUESTIONS.items())
        if isinstance(question, dict) and isinstance(question.get("validator"), str)
        for match in [_BAND_LITERAL.search(question["validator"])]
        if match
    ]
    assert not offenders, (
        "validators restate a reserved address band instead of iterating "
        "reserved_address_bands:\n  " + "\n  ".join(offenders)
    )


def _dns_message(answer: str) -> str:
    return _validator_message(
        "upstream_dns_servers",
        upstream_dns_servers=answer,
        lan_prefix="192.168.0",
        **_address_shared(),
        **_vip_labels(),
        **VIP_ANSWERS,
    )


@pytest.mark.parametrize("answer", sorted(COMPOSED_BANDS))
def test_upstream_dns_servers_rejects_the_composed_address_bands(answer):
    """A resolver's vmid is DERIVED from its address (100 + last octet), so an
    answer inside a composed band duplicates the address AND the vmid — and pct
    and qm share one vmid namespace. Both halves fail phases after the answer."""
    assert _dns_message(answer), (
        f"{answer} ({COMPOSED_BANDS[answer]}) was accepted; it collides with an "
        "address hosts.yml composes"
    )


@pytest.mark.parametrize("name,address", sorted(VIP_ANSWERS.items()))
def test_upstream_dns_servers_rejects_the_vips_and_the_gateway(name, address):
    assert _dns_message(address), f"{address} was accepted, but it is {name}"


@pytest.mark.parametrize(
    "answer",
    ["192.168.0.21 192.168.0.22", "192.168.0.20", "192.168.0.30", "192.168.0.60 192.168.0.61"],
)
def test_upstream_dns_servers_accepts_free_addresses(answer):
    """The bands must not swallow the whole LAN — a false rejection here is an
    operator blocked at the prompt with nowhere to go."""
    assert not _dns_message(answer), f"{answer} was rejected but collides with nothing"


def test_upstream_dns_servers_rejects_a_repeated_address():
    assert _dns_message("192.168.0.21 192.168.0.21"), "the same address twice was accepted"


def _compute_count_message(count: int, resolvers: str = "192.168.0.21 192.168.0.22") -> str:
    return _validator_message(
        "compute_node_count",
        compute_node_count=count,
        upstream_dns_servers=resolvers,
        lan_prefix="192.168.0",
        **_address_shared(),
        **_vip_labels(),
        **VIP_ANSWERS,
    )


@pytest.mark.parametrize("count", [1, 2, 4, 8])
def test_compute_node_count_accepts_the_counts_the_scheme_covers(count):
    """Eight compute hosts is the ceiling the agent band allows, so every count
    up to it must pass — a false rejection is an operator stuck at the prompt."""
    assert not _compute_count_message(count), f"{count} was rejected but composes free addresses"


def test_compute_node_count_rejects_zero():
    assert _compute_count_message(0), "0 was accepted, but the NAS node alone is not a quorum"


def test_compute_node_count_stops_at_the_agent_band_ceiling():
    """One agent per Proxmox host at .41+, so count n puts the last agent at
    .41+n. At 9 that is .50, which the scheme leaves for application guests."""
    message = _compute_count_message(9)
    assert message, "9 was accepted, but its 10th agent lands outside the .41-.49 band"
    assert "192.168.0.50" in message, f"the message does not name the address that overflows: {message}"


def test_the_agent_band_ceiling_survives_a_new_reserved_band():
    """The bands list is documented as extensible, so the ceiling must bind to
    the .41 band by identity rather than to whichever entry is last."""
    shared = _address_shared()
    shared["reserved_address_bands"] = shared["reserved_address_bands"] + [
        [60, 69, "an application guest band added later"]
    ]
    message = _validator_message(
        "compute_node_count",
        compute_node_count=9,
        upstream_dns_servers="192.168.0.21 192.168.0.22",
        lan_prefix="192.168.0",
        **shared,
        **_vip_labels(),
        **VIP_ANSWERS,
    )
    assert message, "9 was accepted once a band above .49 was appended"
    assert "192.168.0.50" in message, f"the ceiling moved to the new band: {message}"


@pytest.mark.parametrize(
    "count,expected",
    [
        (10, "a resolver"),        # pve-node-10 at .21
        (12, "the SMTP relay"),    # pve-node-12 at .23
        (20, "k3s server band"),
    ],
)
def test_compute_node_count_rejects_counts_that_reach_a_claimed_address(count, expected):
    """The compute band grows upward from .12 into addresses hosts.yml already
    composes; each collision must be named, not just counted."""
    message = _compute_count_message(count)
    assert message, f"{count} was accepted despite reaching {expected}"
    assert expected in message, f"the message does not say why {count} collides: {message}"


@pytest.mark.parametrize("name", sorted(VIP_ANSWERS))
def test_compute_node_count_rejects_a_count_that_reaches_a_vip(name):
    """The VIPs and the gateway are answered above this question, so a compute
    host landing on one is comparable here. Each VIP is moved onto pve-node-02's
    address in turn; the shipped defaults sit outside the band.
    """
    collide = "192.168.0.13"  # pve-node-02, the second host of a count-2 render
    vips = dict(VIP_ANSWERS, **{name: collide})
    message = _validator_message(
        "compute_node_count",
        compute_node_count=2,
        upstream_dns_servers="192.168.0.21 192.168.0.22",
        lan_prefix="192.168.0",
        **_address_shared(),
        **_vip_labels(vips),
        **vips,
    )
    assert message, f"{collide} was accepted as a compute host, but it is {name}"
    assert collide in message, f"the message does not name the colliding address: {message}"


# Overlap is decided by integer prefix arithmetic in Jinja, not by string
# comparison, so a mask-boundary case is the only thing that proves it works.
@pytest.mark.parametrize(
    "pod,lan,rejected",
    [
        ("10.42.0.0/16", "192.168.0.0/24", False),   # the shipped default
        ("10.42.0.0/16", "10.42.5.0/24", True),      # LAN inside the pod range
        ("10.42.0.0/16", "10.0.0.0/8", True),        # pod range inside a 10/8 LAN
        ("10.42.0.0/16", "10.43.0.0/16", False),     # adjacent, not overlapping
        ("notacidr", "192.168.0.0/24", True),        # shape rejected before the math
    ],
)
def test_pod_cidr_must_not_overlap_the_lan(pod, lan, rejected):
    message = _validator_message("k3s_pod_cidr", k3s_pod_cidr=pod, lan_cidr=lan)
    assert bool(message) is rejected, f"pod {pod} vs LAN {lan}: {message or 'accepted'}"


@pytest.mark.parametrize(
    "service,rejected",
    [
        ("10.43.0.0/16", False),   # the shipped default
        ("10.42.128.0/17", True),  # inside the pod range
        ("192.168.0.0/24", True),  # the LAN itself
        ("10.42.0.0/15", True),    # supernet swallowing the pod range
    ],
)
def test_service_cidr_must_not_overlap_the_pod_range_or_the_lan(service, rejected):
    message = _validator_message(
        "k3s_service_cidr",
        k3s_service_cidr=service,
        k3s_pod_cidr="10.42.0.0/16",
        lan_cidr="192.168.0.0/24",
    )
    assert bool(message) is rejected, f"service {service}: {message or 'accepted'}"


@pytest.mark.parametrize("name", ["k3s_api_vip", "metallb_public_vip", "metallb_internal_vip"])
def test_vips_are_tested_against_the_whole_lan_cidr_not_a_prefix(name):
    """A /16 LAN has 256 usable third octets; membership is a mask test, so a VIP
    outside `lan_prefix`'s /24 is legal and must be accepted."""
    context = {
        "lan_cidr": "172.20.0.0/16",
        "lan_prefix": "172.20.0",
        **_address_shared("172.20.0.0/16"),
    }
    assert not _validator_message(name, **{name: "172.20.9.161"}, **context)
    assert _validator_message(name, **{name: "10.1.1.161"}, **context)


# The address questions, each with the answers its validator compares against.
# Ordered as copier asks them, so every entry names only earlier answers.
ADDRESS_QUESTIONS = {
    "lan_gateway": {},
    "k3s_api_vip": {"lan_gateway": "192.168.0.1"},
    "metallb_public_vip": {"lan_gateway": "192.168.0.1", "k3s_api_vip": "192.168.0.161"},
    "metallb_internal_vip": {
        "lan_gateway": "192.168.0.1",
        "k3s_api_vip": "192.168.0.161",
        "metallb_public_vip": "192.168.0.100",
    },
}


def _computed(name: str, **context):
    """Render one `when: false` computed value the way copier does — its default
    is evaluated once and is in scope for every question below it."""
    question = QUESTIONS[name]
    rendered = copier_env().from_string(str(question["default"])).render(**context)
    return yaml.safe_load(rendered) if question.get("type") == "yaml" else rendered.strip()


def _address_shared(lan_cidr: str = "192.168.0.0/24") -> dict:
    """The computed values the address validators read. Rendered from copier.yml
    rather than restated, so the shared block is under test too; the note reads
    the bands, as it does in copier's own scope."""
    bands = _computed("reserved_address_bands")
    return {
        "reserved_address_bands": bands,
        "reserved_address_note": _computed("reserved_address_note", reserved_address_bands=bands),
        "lan_address_range": _computed("lan_address_range", lan_cidr=lan_cidr),
    }


def _address_message(name: str, answer: str) -> str:
    return _validator_message(
        name,
        **{name: answer},
        lan_cidr="192.168.0.0/24",
        lan_prefix="192.168.0",
        **_address_shared("192.168.0.0/24"),
        **ADDRESS_QUESTIONS[name],
    )


@pytest.mark.parametrize("name", sorted(ADDRESS_QUESTIONS))
@pytest.mark.parametrize("answer", sorted(COMPOSED_BANDS))
def test_address_answers_reject_the_composed_bands(name, answer):
    """hosts.yml composes guest addresses from fixed bands, so a VIP or gateway
    inside one is an address a generated guest also takes. The VIPs are not
    inventory hosts, so nothing downstream compares the two.
    """
    message = _address_message(name, answer)
    assert message, (
        f"{name} accepted {answer} ({COMPOSED_BANDS[answer]}), which hosts.yml "
        "composes a guest onto"
    )
    assert answer in message, f"the message does not name the address: {message}"


@pytest.mark.parametrize("name", sorted(ADDRESS_QUESTIONS))
@pytest.mark.parametrize("answer", ["192.168.0.1", "192.168.0.20", "192.168.0.30", "192.168.0.161"])
def test_address_answers_accept_addresses_outside_the_bands(name, answer):
    """The bands must not swallow the LAN — a false rejection is an operator
    stuck at the prompt. Each answer is tested as the FIRST of its group, so the
    inequality checks against earlier answers do not confound the band check."""
    context = {k: v for k, v in ADDRESS_QUESTIONS[name].items() if v != answer}
    message = _validator_message(
        name,
        **{name: answer},
        lan_cidr="192.168.0.0/24",
        lan_prefix="192.168.0",
        **_address_shared("192.168.0.0/24"),
        **context,
    )
    assert not message, f"{name} rejected {answer}, which collides with nothing: {message}"


def _lan_prefix_message(lan_cidr: str, prefix: str) -> str:
    return _validator_message(
        "lan_prefix",
        lan_prefix=prefix,
        lan_cidr=lan_cidr,
        lan_address_range=_computed("lan_address_range", lan_cidr=lan_cidr),
        reserved_address_bands=_computed("reserved_address_bands"),
    )


@pytest.mark.parametrize(
    "lan_cidr,prefix,rejected",
    [
        ("192.168.0.0/24", "192.168.0", False),
        ("172.20.0.0/16", "172.20.9", False),    # any third octet of a /16
        ("192.168.0.0/26", "192.168.0", False),  # ends at .63, clear of the roster
        ("192.168.0.0/24", "192.168.1", True),   # the neighbouring /24
        ("192.168.0.0/24", "10.0.0", True),
        ("192.168.0.0/24", "192.168.0.", True),  # shape
        ("192.168.0.0/28", "192.168.0", True),   # ends at .15, under the roster
        ("192.168.0.0/27", "192.168.0", True),   # ends at .31, under the roster
    ],
)
def test_lan_prefix_must_sit_inside_the_lan(lan_cidr, prefix, rejected):
    """lan_prefix composes every host, guest and scrape target in the starter
    roster, so a prefix outside lan_cidr puts the whole inventory off the network
    the firewall sets and the NFS export allowlist are derived from."""
    message = _lan_prefix_message(lan_cidr, prefix)
    assert bool(message) is rejected, f"{prefix} in {lan_cidr}: {message or 'accepted'}"


def test_a_lan_too_narrow_for_the_roster_is_rejected_by_its_top_address():
    """The `.1` address fits every prefix length, so checking it alone accepts a
    LAN that the roster's own k3s agent band already overruns."""
    bands = _computed("reserved_address_bands")
    top = max(hi for _lo, hi, _name in bands)
    message = _lan_prefix_message("192.168.0.0/28", "192.168.0")
    assert f"192.168.0.{top}" in message, (
        f"the message does not name the address the roster reaches: {message}"
    )
    assert "192.168.0.0/28" in message, f"the message does not name the prefix: {message}"


@pytest.mark.parametrize(
    "answer,rejected",
    [
        ("Homelab", False),
        ("My Cluster Vault", False),
        ("", True),
        ("   ", True),
        ("infra/homelab", True),   # re-splits every op:// URI
        ("Homelab: Prod", True),   # splits the unquoted YAML key it renders as
        (" Homelab", True),        # emitted verbatim into the URI
        ("Homelab ", True),
    ],
)
def test_onepassword_vault_must_be_one_uri_segment(answer, rejected):
    """The answer is the first path segment of `op://<vault>/<item>/<field>` and
    is interpolated raw everywhere. A slash re-splits the URI; a colon splits
    the unquoted YAML key the ClusterSecretStore renders it as.
    """
    message = _validator_message("onepassword_vault", onepassword_vault=answer)
    assert bool(message) is rejected, f"{answer!r}: {message or 'accepted'}"


def _fqdn_context(name: str) -> dict:
    """What the service-FQDN validators compare an answer against: the roster
    labels it may not reuse, plus nas_host, which smtp_host checks itself
    against."""
    context = {
        "internal_domain": "lan.example.com",
        "roster_label_re": _computed("roster_label_re"),
    }
    if name != "nas_host":
        context["nas_host"] = "nas-01.lan.example.com"
    return context


@pytest.mark.parametrize("name", ["nas_host", "smtp_host"])
def test_service_fqdns_must_sit_under_the_internal_domain(name):
    """The answer must sit inside internal_domain: the wildcard certificate
    covers `*.<internal_domain>` only, and the NFS PVs verify its SAN with
    `xprtsec=tls`."""
    inside = _validator_message(name, **{name: "box.lan.example.com"}, **_fqdn_context(name))
    outside = _validator_message(name, **{name: "box.example.com"}, **_fqdn_context(name))
    assert not inside, f"{name} rejected a name inside internal_domain"
    assert outside, f"{name} accepted a name outside internal_domain"


@pytest.mark.parametrize("name", ["nas_host", "smtp_host"])
def test_service_fqdns_must_be_one_label_under_the_internal_domain(name):
    """A DNS wildcard matches one label, so `*.<internal_domain>` does not cover
    a deeper name — and the inventory keeps only the short name, so the two
    layers disagree as well."""
    message = _validator_message(
        name, **{name: "box.rack1.lan.example.com"}, **_fqdn_context(name)
    )
    assert message, f"{name} accepted a two-label name the wildcard cannot cover"
    assert "box.lan.example.com" in message, (
        f"the message does not name the one-label form to use: {message}"
    )


@pytest.mark.parametrize(
    "nas,smtp,rejected",
    [
        ("nas-01.lan.example.com", "smtp-relay.lan.example.com", False),
        ("store.lan.example.com", "store.lan.example.com", True),
        ("nas-01.lan.example.com", "dns-01.lan.example.com", True),
    ],
)
def test_service_short_names_must_not_collide(nas, smtp, rejected):
    """Both answers' short names are inventory host names. A repeat puts one name
    under two groups, and Ansible merges them into a single machine that the
    storage and the mail plays both target."""
    message = _validator_message(
        "smtp_host",
        smtp_host=smtp,
        nas_host=nas,
        internal_domain="lan.example.com",
        roster_label_re=_computed("roster_label_re"),
    )
    assert bool(message) is rejected, f"{nas} / {smtp}: {message or 'accepted'}"


def test_nas_host_rejects_a_label_the_roster_generates():
    """nas_host is asked first, so its guard is against the labels the starter
    roster composes rather than against smtp_host."""
    context = _fqdn_context("nas_host")
    assert _validator_message("nas_host", nas_host="k3s-srv-01.lan.example.com", **context), (
        "a label the roster already generates was accepted"
    )
    assert not _validator_message("nas_host", nas_host="nas-01.lan.example.com", **context)


def test_tailnet_dns_suffix_rejects_its_own_placeholder():
    """It is only asked once `vpn_tailscale` is true — the operator has already
    said they have a tailnet, so they can name it. A sentinel that passes its own
    validator ships a resolver CNAMEing into a domain that does not exist."""
    assert _validator_message("tailnet_dns_suffix", tailnet_dns_suffix="CHANGEME.ts.net"), (
        "the CHANGEME placeholder was accepted"
    )
    assert not _validator_message("tailnet_dns_suffix", tailnet_dns_suffix="tail1a2b3c.ts.net"), (
        "a real MagicDNS suffix was rejected"
    )


def test_tailnet_dns_suffix_has_no_default():
    """A `default:` here is answered by pressing enter; there is no value that is
    right for two different tailnets."""
    assert "default" not in QUESTIONS["tailnet_dns_suffix"], (
        "tailnet_dns_suffix carries a default again — use `placeholder:`, which "
        "copier shows as a hint and never accepts as an answer"
    )


# --------------------------------------------------------------------------
# The gitleaks configs extend the default ruleset
# --------------------------------------------------------------------------

# This repository's own allowlist and the one every generated cluster renders.
GITLEAKS_CONFIGS = (
    REPO_ROOT / ".gitleaks.toml",
    TEMPLATE_ROOT / (".gitleaks.toml" + TEMPLATES_SUFFIX),
)

_JINJA_STATEMENT = re.compile(r"^\s*\{%-?.*-?%\}\s*$")


def _extends_default_rules(text: str) -> bool:
    """Whether `[extend] useDefault` is true.

    Jinja statement lines are dropped so the template copy parses as the TOML it
    renders into; the answers gate allowlist entries, never `[extend]`.
    """
    body = "\n".join(line for line in text.splitlines() if not _JINJA_STATEMENT.match(line))
    return (tomllib.loads(body).get("extend") or {}).get("useDefault") is True


@pytest.mark.parametrize("path", GITLEAKS_CONFIGS, ids=lambda p: p.parent.name)
def test_gitleaks_extends_the_default_rules(path):
    """Without `[extend] useDefault` gitleaks loads only the rules in the file,
    and it declares none: every secret scan then greens on any tree."""
    assert path.is_file(), f"{path} is missing — this gate read nothing"
    assert _extends_default_rules(path.read_text(encoding="utf-8")), (
        f"{path}: [extend] useDefault is not true — secret detection is disabled"
    )


def test_a_gitleaks_config_without_extend_is_reported():
    """The negative case: the helper must reject the disarmed file."""
    assert not _extends_default_rules('title = "x"\n[[allowlists]]\ndescription = "y"\n')


# --------------------------------------------------------------------------
# The library pin — one value, three places
# --------------------------------------------------------------------------


def test_lib_ref_is_inherited_by_the_validated_fixture():
    """The fixture does not answer lib_ref, so the render inherits copier.yml's
    default and validate-rendered-cluster exercises the released pin by construction."""
    fixture = yaml.safe_load((REPO_ROOT / "tests" / "answers-weisssrv-shaped.yml").read_text())
    assert "lib_ref" not in fixture, (
        "answers-weisssrv-shaped.yml should inherit lib_ref from copier.yml's "
        "default (the single source), not restate it"
    )


def test_this_repository_applies_its_own_lib_pin_gate():
    """The gate the template ships must hold on the template's own pipeline.

    No job runs the checker here, so this runs the vendored checker over this
    repository's includes and ties WEISSSRV_LIB_REF to copier.yml's lib_ref.
    """
    checker = load_script("check-lib-pins.py")

    ci_file = REPO_ROOT / ".gitlab-ci.yml"
    variables = checker.load_ci(ci_file).get("variables") or {}
    project = variables.get("LIB_PROJECT") or checker.LIB_PROJECT
    problems = checker.check(ci_file, project)
    assert problems == [], "\n".join(problems)
    assert variables.get("WEISSSRV_LIB_REF") == QUESTIONS["lib_ref"]["default"], (
        "variables.WEISSSRV_LIB_REF and copier.yml's lib_ref default disagree — "
        "validate-rendered-cluster clones the latter, so the includes would be gated "
        "against a library this repository never exercises"
    )


def test_copier_pin_is_the_same_in_both_places_this_pipeline_installs_it():
    """`variables.COPIER_VERSION` and the `pip_packages:` literal must agree.

    Includes resolve before job variables exist, so the python-tests entry
    repeats the pin; a disagreement renders the two jobs under different copiers.
    """
    checker = load_script("check-lib-pins.py")

    ci = checker.load_ci(REPO_ROOT / ".gitlab-ci.yml")
    pinned = (ci.get("variables") or {})["COPIER_VERSION"]
    literals = [
        package
        for include in ci["include"]
        if isinstance(include, dict)
        for package in str((include.get("inputs") or {}).get("pip_packages", "")).split()
        if package.startswith("copier==")
    ]
    assert literals, "no include installs copier — this gate examined nothing"
    for literal in literals:
        assert literal == f"copier=={pinned}", (
            f"{literal} disagrees with variables.COPIER_VERSION ({pinned})"
        )


def test_lib_ref_validator_takes_release_tags_only():
    """The include contract forbids a branch pin: a branch deleted after merge
    takes every include with it. A tag below the default is refused too: the
    playbooks are written against that release's role contracts."""
    assert not _validator_message("lib_ref", lib_ref=QUESTIONS["lib_ref"]["default"])
    for rejected in ("main", "chore/some-branch", "0.6.0", "v0.6", "v0.6.0-rc1"):
        assert _validator_message("lib_ref", lib_ref=rejected), (
            f"lib_ref accepted {rejected!r}, which is not a release tag"
        )
    floor = tuple(int(part) for part in QUESTIONS["lib_ref"]["default"][1:].split("."))
    older = (
        f"v{floor[0]}.{floor[1]}.{floor[2] - 1}"
        if floor[2]
        else f"v{floor[0]}.{floor[1] - 1}.0"
    )
    assert _validator_message("lib_ref", lib_ref=older), (
        f"lib_ref accepted {older!r}, which is older than the pinned default"
    )
    assert not _validator_message("lib_ref", lib_ref=f"v{floor[0]}.{floor[1] + 1}.0")


_TAG_LITERAL = re.compile(r"\bv\d+\.\d+\.\d+\b")


def _is_historical(path: Path, line: str) -> bool:
    """Lines that record what a PAST release pinned, not what to pin now.

    A docs/VERSIONING.md pair row naming a released tag, or a `_commit:` marker
    in an answers example. The `main` row names the live pin, so it stays in scope.
    """
    stripped = line.strip()
    if stripped.startswith("_commit:"):
        return True
    if not stripped.startswith("|") or path.name != "VERSIONING.md":
        return False
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return len(cells) > 1 and "main" not in cells[0] and bool(_TAG_LITERAL.search(cells[0]))


def test_docs_quote_only_the_current_library_tag():
    """The docs name the library tag as a literal in several places. Nothing else
    ties them to the answer default, so a bump that misses one leaves a
    copy-pasteable command pinned to a superseded release."""
    want = QUESTIONS["lib_ref"]["default"]
    stale = []
    for path in [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _is_historical(path, line):
                continue
            for tag in _TAG_LITERAL.findall(line):
                if tag != want:
                    stale.append(f"{path.relative_to(REPO_ROOT)}:{lineno} {tag}")
    assert not stale, (
        f"docs quote a library tag other than copier.yml's lib_ref default ({want}):\n  "
        + "\n  ".join(stale)
    )


def test_only_released_pair_rows_are_exempt_from_the_stale_tag_scan():
    """Mutation case: the `main` row and the other tables stay in scope, so a
    bump that misses the pin it records reports."""
    versioning = Path("docs/VERSIONING.md")
    assert _is_historical(versioning, "| `v0.8.0` | weisssrv-lib `v0.13.0` |")
    assert not _is_historical(versioning, "| `main` (unreleased) | weisssrv-lib `v0.1.0` |")
    assert not _is_historical(versioning, "| `feat:` | MINOR, pins `v0.1.0` |")
    assert not _is_historical(Path("README.md"), "| `v0.8.0` | weisssrv-lib `v0.13.0` |")


def _tags() -> list[str]:
    """Release tags in this checkout, newest last. Empty on a shallow clone."""
    listed = subprocess.run(
        ["git", "tag", "-l", "v*"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        return []

    def key(tag: str) -> tuple:
        return tuple(int(part) if part.isdigit() else 0 for part in tag.lstrip("v").split("."))

    return sorted(listed.stdout.split(), key=key)


def _questions_at(ref: str) -> dict | None:
    """copier.yml's questions at `ref`, or None when the checkout cannot show it."""
    shown = subprocess.run(
        ["git", "show", f"{ref}:copier.yml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode != 0 or not shown.stdout.strip():
        return None
    doc = yaml.safe_load(shown.stdout) or {}
    return {name: value for name, value in doc.items() if not name.startswith("_")}


def breaking_answer_changes(old: dict, new: dict) -> dict[str, str]:
    """question -> why a generated repo cannot take this update unattended.

    A recorded answer is replayed on every `copier update`: a dropped name binds
    nothing, and a moved enumerated default renders a branch nobody chose.
    """
    changes: dict[str, str] = {}
    for name, before in old.items():
        if name not in new:
            changes[name] = "removed or renamed: the recorded answer binds nothing"
            continue
        after = new[name]
        if not (isinstance(before, dict) and isinstance(after, dict)):
            continue
        enumerated = before.get("type") == "bool" or before.get("choices")
        if enumerated and before.get("default") != after.get("default"):
            changes[name] = (
                f"default moved from {before.get('default')!r} to {after.get('default')!r}: "
                "a new cluster renders the other branch"
            )
    return changes


def test_a_breaking_answer_schema_change_is_recorded():
    """docs/VERSIONING.md calls the answer schema public API, and nothing holds a
    rename to that claim: an operator's `copier update` is where it surfaces."""
    tags = _tags()
    if not tags:
        if os.environ.get("CI"):
            pytest.fail("no tags in this checkout, so the answer schema went unchecked")
        pytest.skip("no tags in this checkout (shallow clone)")
    released = _questions_at(tags[-1])
    if released is None:
        pytest.skip(f"{tags[-1]}:copier.yml is not in this checkout")

    changes = breaking_answer_changes(released, QUESTIONS)
    versioning = (REPO_ROOT / "docs" / "VERSIONING.md").read_text(encoding="utf-8")
    undocumented = {
        name: why for name, why in changes.items() if f"`{name}`" not in versioning
    }
    assert not undocumented, (
        f"copier.yml changed the answer schema since {tags[-1]} without naming the "
        "question in docs/VERSIONING.md:\n  "
        + "\n  ".join(f"{name}: {why}" for name, why in sorted(undocumented.items()))
    )


def test_an_undocumented_question_rename_is_caught():
    """Mutation case: a rename and a flipped boolean default both report."""
    changes = breaking_answer_changes(
        {"cluster_name": {"type": "str"}, "gpu": {"type": "bool", "default": True}},
        {"cluster_label": {"type": "str"}, "gpu": {"type": "bool", "default": False}},
    )
    assert set(changes) == {"cluster_name", "gpu"}
    assert "binds nothing" in changes["cluster_name"]
    assert "renders the other branch" in changes["gpu"]


def _version(tag: str) -> tuple[int, ...]:
    """A `vMAJOR.MINOR.PATCH` tag as a sortable tuple."""
    return tuple(int(part) for part in tag.lstrip("v").split(".") if part.isdigit())


def test_every_template_release_has_a_validated_pair_row():
    """Every released template tag has a validated-pair row in docs/VERSIONING.md.

    The table is the only record of the library release a tag was rendered
    against, and nothing at tag time writes the row. The newest tag is exempt.
    """
    table = (REPO_ROOT / "docs" / "VERSIONING.md").read_text(encoding="utf-8")
    rows = {
        cell.strip().strip("`")
        for line in table.splitlines()
        if line.strip().startswith("|")
        for cell in [line.split("|")[1]]
    }
    assert any(row.startswith("main") for row in rows), (
        "docs/VERSIONING.md has no `main` row — the newest tag's exemption rests "
        "on that row being its pair"
    )

    tags = subprocess.run(
        ["git", "tag", "-l", "v*"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tags.returncode == 0, (
        "git could not list tags, so this gate verified nothing: " + tags.stderr.strip()
    )
    if not tags.stdout.strip():
        if os.environ.get("CI"):
            pytest.fail("no tags in this checkout, so the validated-pair table went unchecked")
        pytest.skip("no tags in this checkout (shallow clone)")

    released = sorted(tags.stdout.split(), key=_version)
    missing = [tag for tag in released[:-1] if tag not in rows]
    assert not missing, (
        "docs/VERSIONING.md's validated-pair table has no row for: "
        + ", ".join(missing)
    )


# One accept and one reject per ARM of every validator no other test exercises.
# `context` supplies only the answers the validator reads; the optional fifth
# element is a substring the rejection message must contain.
VALIDATOR_CASES = [
    ("cluster_name", "homelab", None, {}),
    ("cluster_name", "Homelab", "uppercase is not a DNS label", {}),
    ("cluster_name", "1homelab", "must start with a letter", {}),
    ("cluster_name", "ho", "shorter than three characters", {}),
    ("internal_domain", "lan.example.com", None, {}),
    ("internal_domain", "LAN.example.com", "uppercase", {}),
    ("internal_domain", "lan", "not fully qualified", {}),
    ("external_domain", "example.com", None, {"internal_domain": "lan.example.com"}),
    (
        "external_domain",
        "EXAMPLE.com",
        "uppercase is not a DNS label",
        {"internal_domain": "lan.example.com"},
        "must be a lowercase fully-qualified domain",
    ),
    (
        "external_domain",
        "lan.example.com",
        "one zone answered twice: both wildcard Certificates claim the same "
        "identifiers and both halves of every split-horizon route match the same host",
        {"internal_domain": "lan.example.com"},
        "must differ from internal_domain",
    ),
    ("admin_user", "ops", None, {}),
    (
        "admin_user",
        "root",
        "SSH hardening disables root login, locking Ansible out",
        {},
        "must not be root",
    ),
    (
        "admin_user",
        "0ps",
        "a POSIX username may not start with a digit",
        {},
        "must be a valid POSIX username",
    ),
    ("admin_email", "ops@example.com", None, {}),
    ("admin_email", "ops@example", "no TLD", {}),
    ("alert_email", "pager@example.com", None, {}),
    ("alert_email", "pager", "not an address", {}),
    ("timezone", "UTC", None, {}),
    ("timezone", "Europe/Berlin", None, {}),
    ("timezone", "PST", "not an IANA name", {}),
    ("license_holder", "Ada Lovelace", None, {"license": "mit"}),
    (
        "license_holder",
        "   ",
        "an unnamed copyright holder",
        {"license": "mit"},
        "license_holder is required",
    ),
    ("license_holder", "", None, {"license": "none"}),
    ("license_year", "2026", None, {"license": "mit"}),
    ("license_year", "26", "a two-digit year", {"license": "mit"}, "four-digit year"),
    ("license_year", "", None, {"license": "none"}),
    ("git_host", "git.example.com", None, {}),
    ("git_host", "https://git.example.com", "a scheme is not a hostname", {}),
    ("git_namespace", "homelab/infra", None, {}),
    ("git_namespace", "/homelab", "leading slash", {}),
    ("lib_url", "https://git.example.com/group/weisssrv-lib.git", None, {}),
    ("lib_url", "git@git.example.com:group/weisssrv-lib.git", "ssh form, not http(s)", {}),
    ("lib_project", "group/weisssrv-lib", None, {}),
    ("lib_project", "weisssrv-lib", "no namespace segment", {}),
    ("ci_runner_tag", "infrastructure", None, {}),
    ("ci_runner_tag", "   ", "blank leaves the job on the untagged runner", {}),
    ("ci_cpu_selector", "lan.example.com/cpu=modern", None, {"internal_domain": "lan.example.com"}),
    (
        "ci_cpu_selector",
        "zone=fast",
        "the runners' node_selector_overwrite_allowed regex refuses it at pod creation",
        {"internal_domain": "lan.example.com"},
    ),
    (
        "ci_cpu_selector",
        "lan.example.com/cpu=fast",
        "only modern and legacy are allowed values",
        {"internal_domain": "lan.example.com"},
    ),
    ("k3s_image_gc_high_threshold", 70, None, {}),
    ("k3s_image_gc_high_threshold", 79, None, {}),
    (
        "k3s_image_gc_high_threshold",
        50,
        "at the 50% low watermark the kubelet garbage-collects on every pull",
        {},
        "must be at least 55",
    ),
    (
        "k3s_image_gc_high_threshold",
        80,
        "from 80 up the DiskUsageWarning inhibit mutes KubeletImageGCIneffective",
        {},
        "must be at most 79",
    ),
    ("compose_app_guests", [], None, {}),
    (
        "compose_app_guests",
        [{"host": "10.0.0.56", "label": "nextcloud", "compose_dir": "/opt/nc"}],
        None,
        {},
    ),
    (
        "compose_app_guests",
        [{"host": "10.0.0.56", "label": "nextcloud"}],
        "compose_dir is what the collector cds into; without it the section is empty",
        {},
        "needs host, label and compose_dir",
    ),
    (
        "compose_app_guests",
        [{"host": "10.0.0.56", "label": "NextCloud", "compose_dir": "/opt/nc"}],
        "the label becomes a report section name and a run_section label",
        {},
        "must be a lowercase slug",
    ),
    (
        "compose_app_guests",
        [{"host": "10.0.0.56", "label": "nc", "compose_dir": "/opt/$nc"}],
        "the value is interpolated into the collector's remote `sh -c` strings, "
        "where the inner shell expands it",
        {},
        "must not contain a quote, $ or a backtick",
    ),
    (
        "compose_app_guests",
        [{"host": "10.0.0.56", "label": "nc", "compose_dir": "/opt/my nc"}],
        "the collector interpolates each value unquoted into a remote word, so a "
        "space makes it two",
        {},
        "must not contain a space",
    ),
]


@pytest.mark.parametrize(
    "case",
    VALIDATOR_CASES,
    ids=[f"{c[0]}-{c[1]}" for c in VALIDATOR_CASES],
)
def test_validator_accepts_and_rejects(case):
    """A case may carry a fifth element: a substring the message must contain,
    which is how a validator with several arms proves it took the right one."""
    name, answer, why, context = case[:4]
    expected = case[4] if len(case) > 4 else None
    message = _validator_message(name, **{name: answer}, **context)
    if why is None:
        assert not message, f"{name}={answer!r} was rejected: {message}"
    else:
        assert message, f"{name}={answer!r} was accepted — {why}"
        if expected:
            assert expected in message, (
                f"{name}={answer!r} was rejected by the wrong arm: {message}"
            )


# Validators with a test of their own above, which the table deliberately does
# not duplicate — the value is in the reasoning those tests carry, not in a
# second accept/reject pair.
DEDICATED_VALIDATOR_TESTS = {
    "lan_cidr": "test_pod_cidr_must_not_overlap_the_lan",
    "lan_prefix": "test_lan_prefix_must_sit_inside_the_lan",
    "lan_gateway": "test_address_answers_*",
    "k3s_api_vip": "test_address_answers_*",
    "metallb_public_vip": "test_address_answers_*",
    "metallb_internal_vip": "test_address_answers_*",
    "k3s_pod_cidr": "test_pod_cidr_must_not_overlap_the_lan",
    "k3s_service_cidr": "test_service_cidr_must_not_overlap_the_pod_range_or_the_lan",
    "upstream_dns_servers": "test_upstream_dns_servers_*",
    "compute_node_count": "test_compute_node_count_*",
    "git_backend": "test_unimplemented_backend_choices_fail_at_copy_time",
    "secrets_backend": "test_unimplemented_backend_choices_fail_at_copy_time",
    "storage_backend": "test_unimplemented_backend_choices_fail_at_copy_time",
    "dns_backend": "test_unimplemented_backend_choices_fail_at_copy_time",
    "onepassword_vault": "test_onepassword_vault_must_be_one_uri_segment",
    "nas_host": "test_service_fqdns_* and test_nas_host_rejects_a_label_*",
    "smtp_host": "test_service_fqdns_* and test_service_short_names_must_not_collide",
    "node_exporter_job_regex": "test_node_exporter_job_regex_validator_requires_the_shipped_jobs",
    "tailnet_dns_suffix": "test_tailnet_dns_suffix_*",
    "lib_ref": "test_lib_ref_validator_takes_release_tags_only",
}


def test_every_validator_is_exercised():
    """A validator no test exercises is one a regression could widen to accept
    everything with the suite still green."""
    declared = {
        name
        for name, question in QUESTIONS.items()
        if isinstance(question, dict) and "validator" in question
    }
    covered = {case[0] for case in VALIDATOR_CASES} | set(DEDICATED_VALIDATOR_TESTS)
    assert not declared - covered, (
        "copier.yml validators no test exercises: " + ", ".join(sorted(declared - covered))
    )
    assert not covered - declared, (
        "these names are listed as covered but declare no validator: "
        + ", ".join(sorted(covered - declared))
    )


# Asked free-text answers that deliberately take anything, with the reason no
# validator can narrow them. An entry here needs a reason, not convenience.
UNVALIDATED_FREE_TEXT: dict[str, str] = {}


def _unvalidated_free_text(questions: dict, exempt: dict[str, str]) -> list[str]:
    """Asked `type: str` questions with no `choices:` and no `validator:`."""
    return sorted(
        name
        for name, question in questions.items()
        if isinstance(question, dict)
        and question.get("when") is not False
        and question.get("type") == "str"
        and not question.get("choices")
        and "validator" not in question
        and name not in exempt
    )


def test_every_asked_free_text_answer_is_validated():
    """A `type: str` question with no `choices:` takes whatever is typed, and
    `--data` mode does not even prompt. Without a validator a mistyped domain,
    address or username renders a cluster that looks configured."""
    considered = [
        name
        for name, question in QUESTIONS.items()
        if isinstance(question, dict)
        and question.get("when") is not False
        and question.get("type") == "str"
        and not question.get("choices")
    ]
    assert len(considered) > 20, (
        f"only {len(considered)} free-text questions found — the shape of copier.yml "
        "moved and this gate is checking almost nothing"
    )
    missing = _unvalidated_free_text(QUESTIONS, UNVALIDATED_FREE_TEXT)
    assert not missing, (
        "these asked free-text answers declare no validator: " + ", ".join(missing)
    )


def test_the_free_text_validator_gate_notices_a_dropped_validator():
    """Mutation proof for the gate above: the real question set with one
    validator removed must be reported."""
    name = "internal_domain"
    mutated = {**QUESTIONS, name: {k: v for k, v in QUESTIONS[name].items() if k != "validator"}}
    assert _unvalidated_free_text(mutated, UNVALIDATED_FREE_TEXT) == [name]
    assert _unvalidated_free_text(mutated, {name: "exempt in this call only"}) == []
