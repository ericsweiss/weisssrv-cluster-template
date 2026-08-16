"""copier.yml is the template's API: the answer set is replayed on every
`copier update`, so renaming or dropping a question breaks every generated
cluster. These tests pin the schema and the mechanics the template relies on.
"""

from __future__ import annotations

import re
import subprocess
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

# `when: false` entries: copier evaluates their default and puts it in scope for
# every question below, but never prompts and records nothing in
# .copier-answers.yml. They are shared machinery, not answers, so the gates that
# hold an operator-facing question (documented, answered by the fixtures) do not
# apply to them.
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


@pytest.mark.parametrize("name", sorted(IMPLEMENTED_BACKENDS))
def test_unimplemented_backend_choices_fail_at_copy_time(name):
    """A choice with no implementation must stop the render, not produce a repo
    that looks complete and never reconciles. The validator is what enforces it:
    `--data` bypasses the choice list, and a choice list is not a contract with
    the tree anyway."""
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


def _validator_message(name: str, **context) -> str:
    """Render a question's validator the way copier does: a non-empty result is
    the rejection message, an empty one means the answer is accepted."""
    env = jinja2.Environment()  # noqa: S701 - rendering our own config, no user input
    env.filters = _AnyFilter(env.filters)
    env.filters["regex_search"] = lambda value, pattern: re.search(pattern, str(value))
    return env.from_string(QUESTIONS[name]["validator"]).render(**context).strip()


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
    """The answer is free text, but only values containing BOTH shipped job
    labels are ever correct for an unmodified render — a value that drops one
    renders `job=~"..."` into rules that then match zero series, and a rule
    matching nothing never fires and never alerts anyone to its own silence."""
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


# Answers that need no prose: their inline `help` is self-contained AND nothing
# outside the template has to be arranged before answering them. Everything else
# must be named in an operator doc, because the operator decides it BEFORE the
# first prompt appears. Keep this list short — an entry is documentation given
# up.
DOC_EXEMPT = {
    "lan_prefix": "derived from lan_cidr; the prompt's default is the answer",
    "alert_email": "defaults to admin_email, which the docs cover",
    "enable_semantic_release": "a repo-workflow toggle with no external prerequisite",
}

_DOCS = ("PRE-SETUP.md", "SETUP.md")


def test_every_question_is_named_in_an_operator_doc():
    """A question named in neither doc is one an operator meets for the first
    time at the prompt, with no chance to have prepared for it — which is how
    `tailnet_dns_suffix` came to ship a sentinel default that passes its own
    validator and leaves the tailnet resolver permanently broken."""
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


def test_answer_fixture_covers_every_question():
    fixture = yaml.safe_load((REPO_ROOT / "tests" / "answers-weisssrv-shaped.yml").read_text())
    conditional = {name for name, q in QUESTIONS.items() if "when" in q}
    # lib_ref is deliberately left unanswered: copier.yml's default is the single
    # source of the library pin, and inheriting it makes render-validate exercise
    # exactly the released tag — so there is no second literal to keep in step.
    inherited = {"lib_ref"}
    missing = set(QUESTIONS) - set(fixture) - conditional - inherited
    assert not missing, (
        f"tests/answers-weisssrv-shaped.yml does not answer {sorted(missing)} — "
        "the render test would silently exercise the default instead"
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
    """A validator can only compare against answers already given.

    `lan_gateway` carried `{% elif lan_gateway in [k3s_api_vip | default(''),
    ...] %}` while all three VIPs are asked AFTER it. Interactively the names
    were undefined, `| default('')` collapsed the test to `x in ['', '', '']`,
    and the check passed everything — while the `--data-file` path used by these
    very tests supplied all answers up front and made it fire. So the harness
    exercised the arm in the one mode where it could work and the operator met it
    in the mode where it could not.

    This is the general form: a check that reads as enforcement and enforces
    nothing on the documented path. Move the comparison DOWN to the later
    question rather than reaching forward from the earlier one.
    """
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


def _dns_message(answer: str) -> str:
    return _validator_message(
        "upstream_dns_servers",
        upstream_dns_servers=answer,
        lan_prefix="192.168.0",
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


@pytest.mark.parametrize(
    "count,expected",
    [
        (10, "a resolver"),        # pve-node-10 at .21
        (12, "the SMTP relay"),    # pve-node-12 at .23
        (20, "the k3s server band"),
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
    """The VIPs and the gateway are answered ABOVE this question, so they are
    comparable here — and a compute host landing on one is a collision hosts.yml
    composes silently.

    The shipped defaults sit outside the compute band, so each VIP is moved onto
    pve-node-02's address in turn: a test parametrized over the defaults alone
    would skip every case and assert nothing.
    """
    collide = "192.168.0.13"  # pve-node-02, the second host of a count-2 render
    vips = dict(VIP_ANSWERS, **{name: collide})
    message = _validator_message(
        "compute_node_count",
        compute_node_count=2,
        upstream_dns_servers="192.168.0.21 192.168.0.22",
        lan_prefix="192.168.0",
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
    env = jinja2.Environment()  # noqa: S701 - rendering our own config, no user input
    env.filters = _AnyFilter(env.filters)
    rendered = env.from_string(str(question["default"])).render(**context)
    return yaml.safe_load(rendered) if question.get("type") == "yaml" else rendered.strip()


def _address_shared(lan_cidr: str) -> dict:
    """The computed values the four address validators read. Rendered from
    copier.yml rather than restated, so the shared block is under test too."""
    return {
        "reserved_address_bands": _computed("reserved_address_bands"),
        "reserved_address_note": _computed("reserved_address_note"),
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
    """The mirror of the upstream_dns_servers and compute_node_count checks:
    hosts.yml composes guest addresses from fixed bands, so a VIP or a gateway
    inside one is an address a generated guest also takes. kube-vip and MetalLB
    answer ARP for it, and nothing downstream compares the two — the VIPs are
    not inventory hosts."""
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
    """The answer is the first path segment of `op://<vault>/<item>/<field>`,
    and ~99 emission sites interpolate it raw. A slash re-splits the URI and
    surfaces as an item-not-found error inside a deploy job, naming nothing; a
    colon splits the unquoted YAML key the ClusterSecretStore renders it as, and
    that failure takes the whole manifest build with it."""
    message = _validator_message("onepassword_vault", onepassword_vault=answer)
    assert bool(message) is rejected, f"{answer!r}: {message or 'accepted'}"


@pytest.mark.parametrize("name", ["nas_host", "smtp_host"])
def test_service_fqdns_must_sit_under_the_internal_domain(name):
    """The internal wildcard covers `*.<internal_domain>` and nothing else, and
    the NFS PVs mount by name with `xprtsec=tls`, which verifies its SAN. A name
    in another zone fails the handshake exactly as an IP mount does, and the
    internal resolver does not answer for it either."""
    inside = _validator_message(
        name, **{name: "box.lan.example.com"}, internal_domain="lan.example.com"
    )
    outside = _validator_message(
        name, **{name: "box.example.com"}, internal_domain="lan.example.com"
    )
    assert not inside, f"{name} rejected a name inside internal_domain"
    assert outside, f"{name} accepted a name outside internal_domain"


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
# The library pin — one value, three places
# --------------------------------------------------------------------------


def test_lib_ref_is_inherited_by_the_validated_fixture():
    """The fixture must NOT answer lib_ref. render-validate clones the library at
    whatever lib_ref the fixture resolves to; by leaving it unanswered the render
    inherits copier.yml's default, so the pin it exercises IS the released one by
    construction — no second literal that could advertise a tag never exercised."""
    fixture = yaml.safe_load((REPO_ROOT / "tests" / "answers-weisssrv-shaped.yml").read_text())
    assert "lib_ref" not in fixture, (
        "answers-weisssrv-shaped.yml should inherit lib_ref from copier.yml's "
        "default (the single source), not restate it"
    )


def test_this_repository_applies_its_own_lib_pin_gate():
    """The gate the template ships must hold on the template's own pipeline.

    `.gitlab-ci.yml` declares `variables.WEISSSRV_LIB_REF` as the single source
    and its comment says `scripts/check-lib-pins.py` fails the pipeline on drift.
    No job runs the checker here, so this is what makes the claim true: the
    vendored checker over this repository's own includes, plus the tie between
    that variable and `copier.yml`'s `lib_ref` default — the value
    `render-validate` actually clones, and the one a generated cluster inherits.
    """
    import importlib.util

    script = REPO_ROOT / "scripts" / "check-lib-pins.py"
    spec = importlib.util.spec_from_file_location("check_lib_pins", script)
    assert spec and spec.loader
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    ci_file = REPO_ROOT / ".gitlab-ci.yml"
    variables = checker.load_ci(ci_file).get("variables") or {}
    project = variables.get("LIB_PROJECT") or checker.LIB_PROJECT
    problems = checker.check(ci_file, project)
    assert problems == [], "\n".join(problems)
    assert variables.get("WEISSSRV_LIB_REF") == QUESTIONS["lib_ref"]["default"], (
        "variables.WEISSSRV_LIB_REF and copier.yml's lib_ref default disagree — "
        "render-validate clones the latter, so the includes would be gated "
        "against a library this repository never exercises"
    )


def test_copier_pin_is_the_same_in_both_places_this_pipeline_installs_it():
    """`variables.COPIER_VERSION` and the `pip_packages:` literal must agree.

    GitLab resolves `include: inputs:` at pipeline-creation time, before job
    variables exist, so the python-tests entry cannot read the variable and
    repeats the pin — the same constraint `include: ref:` has, and the same
    silent failure: the pytest suite would render under one copier and
    render-validate under another, and the disagreement surfaces as a render
    difference nobody can reproduce.
    """
    import importlib.util

    script = REPO_ROOT / "scripts" / "check-lib-pins.py"
    spec = importlib.util.spec_from_file_location("check_lib_pins", script)
    assert spec and spec.loader
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

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
    takes every include, module source and collection install with it."""
    assert not _validator_message("lib_ref", lib_ref=QUESTIONS["lib_ref"]["default"])
    for rejected in ("main", "chore/some-branch", "0.6.0", "v0.6", "v0.6.0-rc1"):
        assert _validator_message("lib_ref", lib_ref=rejected), (
            f"lib_ref accepted {rejected!r}, which is not a release tag"
        )


_TAG_LITERAL = re.compile(r"\bv\d+\.\d+\.\d+\b")


def _is_historical(path: Path, line: str) -> bool:
    """Lines that record what a PAST release pinned, not what to pin now.

    Two shapes: a row of docs/VERSIONING.md's template-release/library-release
    table, and the `_commit:` marker in a `.copier-answers.yml` example. Both
    name superseded tags on purpose.

    Table rows are exempt only in VERSIONING.md — exempting every `|` line would
    also exempt README.md's answer table, where the `lib_ref` default is exactly
    the literal this test exists to keep current.
    """
    stripped = line.strip()
    if stripped.startswith("_commit:"):
        return True
    return stripped.startswith("|") and path.name == "VERSIONING.md"


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


def test_every_template_release_has_a_validated_pair_row():
    """The pair table is the only record of which library release a template
    release was rendered against, and nothing at tag time writes the row — so a
    release cut without relabelling the `main` row leaves the pair unrecorded."""
    tags = subprocess.run(
        ["git", "tag", "-l", "v*"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if tags.returncode != 0 or not tags.stdout.strip():
        pytest.skip("no tags in this checkout (shallow clone)")

    table = (REPO_ROOT / "docs" / "VERSIONING.md").read_text(encoding="utf-8")
    rows = {
        cell.strip().strip("`")
        for line in table.splitlines()
        if line.strip().startswith("|")
        for cell in [line.split("|")[1]]
    }
    missing = [tag for tag in tags.stdout.split() if tag not in rows]
    assert not missing, (
        "docs/VERSIONING.md's validated-pair table has no row for: "
        + ", ".join(missing)
    )


# One accept and one reject per validator that no other test exercises, plus the
# two SEMANTIC rules (a domain that equals the internal one; root as the admin
# user) whose absence is invisible in a render: both produce a repository that
# lints clean and is wrong the first time it is deployed.
#
# `context` supplies only the answers the validator itself reads.
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
        "lan.example.com",
        "identical to internal_domain — one certificate and ingress pair for two zones",
        {"internal_domain": "lan.example.com"},
    ),
    ("admin_user", "ops", None, {}),
    ("admin_user", "root", "SSH hardening disables root login, locking Ansible out", {}),
    ("admin_user", "0ps", "a POSIX username may not start with a digit", {}),
    ("admin_email", "ops@example.com", None, {}),
    ("admin_email", "ops@example", "no TLD", {}),
    ("alert_email", "pager@example.com", None, {}),
    ("alert_email", "pager", "not an address", {}),
    ("timezone", "UTC", None, {}),
    ("timezone", "Europe/Berlin", None, {}),
    ("timezone", "PST", "not an IANA name", {}),
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
]


@pytest.mark.parametrize(
    "name,answer,why,context",
    VALIDATOR_CASES,
    ids=[f"{c[0]}-{c[1]}" for c in VALIDATOR_CASES],
)
def test_validator_accepts_and_rejects(name, answer, why, context):
    message = _validator_message(name, **{name: answer}, **context)
    if why is None:
        assert not message, f"{name}={answer!r} was rejected: {message}"
    else:
        assert message, f"{name}={answer!r} was accepted — {why}"


# Validators with a test of their own above, which the table deliberately does
# not duplicate — the value is in the reasoning those tests carry, not in a
# second accept/reject pair.
DEDICATED_VALIDATOR_TESTS = {
    "lan_cidr": "test_pod_cidr_must_not_overlap_the_lan",
    "lan_prefix": "test_address_answers_* (every case renders it)",
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
    "nas_host": "test_service_fqdns_must_sit_under_the_internal_domain",
    "smtp_host": "test_service_fqdns_must_sit_under_the_internal_domain",
    "node_exporter_job_regex": "test_node_exporter_job_regex_validator_requires_the_shipped_jobs",
    "tailnet_dns_suffix": "test_tailnet_dns_suffix_*",
    "lib_ref": "test_lib_ref_validator_takes_release_tags_only",
}


def test_every_validator_is_exercised():
    """A validator no test exercises is one a regression could widen to accept
    everything with the suite still green — which is how the address family came
    to be the only part of the schema under test."""
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
