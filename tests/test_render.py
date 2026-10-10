"""Render the template with the fixture answers and assert what must hold of
every generated cluster: the answers are recorded, no reference-cluster value
survives, the manifests carry placeholders, and roles are reached by FQCN."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

import jinja2
import pytest
import yaml

import render_cluster
from conftest import copier_env
# The copier-config suite owns the list of exporter jobs the template ships;
# imported rather than restated so the render is checked against THAT list.
from test_copier_config import SHIPPED_EXPORTER_JOBS, TEMPLATE_ROOT, TEMPLATES_SUFFIX

REPO_ROOT = render_cluster.REPO_ROOT

# Reference-cluster literals that must never appear in a generated repository.
# A line also naming an upstream repository is exempt: pinned sources and doc
# links legitimately point at the reference cluster's GitLab.
FORBIDDEN_DOMAINS = ("esweiss.com", "ericsweiss.com")
# The reference cluster's LAN, its Proxmox host names and its admin user. A
# value the answers legitimately spell is exempted per render.
FORBIDDEN_PATTERNS = (
    re.compile(r"(?<![\w.])10\.0\.10\.\d{1,3}(?!\.?\d)"),
    re.compile(r"\bpve-(?:laptop|nas|opt|prec)-\d{2}\b"),
    re.compile(r"(?<![\w.@-])eric(?![\w.-])"),
)
UPSTREAM_REPOS = ("weisssrv-lib", "weisssrv-cluster-template", "weisssrv-app-template")
TEXT_SUFFIXES = {
    ".yml", ".yaml", ".md", ".py", ".sh", ".tf", ".cfg", ".toml", ".json",
    ".j2", ".jinja", ".txt", ".hujson", ".env", ".mdc", ".conf",
}

# Files _text_files() could not read, kept so a drop out of the scans below
# fails instead of reading as a pass.
UNREADABLE: list[tuple[str, str]] = []


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
    """A second render from deliberately unlike answers, with every optional
    module off — see tests/answers-unlike.yml for why it is not optional."""
    scratch = tmp_path_factory.mktemp("render-b")
    return render_cluster.render(scratch, answers=ANSWERS_B, dest_name="render-b")


# answers_b with every optional module ON. The leak scan below looks for
# fixture A's values in a render from fixture B, and the files gated on these
# answers exist in no render it could otherwise see.
MODULES_ON = {
    "vpn_tailscale": True,
    "gpu": "nvidia",
    "use_unifi": True,
    "enable_semantic_release": False,
    "compose_app_guests": [
        {
            "host": "10.77.4.56",
            "label": "ledger",
            "compose_dir": "/opt/ledger/compose",
            "nginx_cert": "/etc/ssl/ledger/fullchain.pem",
            "backup_glob": "/mnt/offsite/ledger-db-*.sql.gz",
            "backup_timer": "ledger-backup.timer",
            "backup_prom": "/var/lib/node_exporter/ledger_backup.prom",
        },
        {
            "host": "10.77.4.58",
            "label": "tidewatch",
            "compose_dir": "/opt/tidewatch/compose",
            "health_url": "http://127.0.0.1:3003/ping",
        },
    ],
}


# Fixture B with ONE optional module on. The shipped fixtures move
# vpn_tailscale, gpu and use_unifi together, so a conditional coupling two of
# them has an arm neither an all-on nor an all-off render ever takes.
MODULES_MIXED = {"vpn_tailscale": True, "gpu": "none", "use_unifi": False}


@pytest.fixture(scope="session")
def answers_b_modules_on(answers_b) -> dict:
    return {**answers_b, **MODULES_ON}


@pytest.fixture(scope="session")
def answers_b_mixed(answers_b) -> dict:
    return {**answers_b, **MODULES_MIXED}


@pytest.fixture(scope="session")
def rendered_b_modules_on(tmp_path_factory, answers_b_modules_on) -> Path:
    """Fixture B's answers with the optional modules on, so the leak scan reaches
    the files those answers gate."""
    scratch = tmp_path_factory.mktemp("render-b-on")
    derived = scratch / "answers-unlike-modules-on.yml"
    derived.write_text(yaml.safe_dump(answers_b_modules_on, sort_keys=False))
    return render_cluster.render(scratch, answers=derived, dest_name="render-b-on")


@pytest.fixture(scope="session")
def rendered_b_mixed(tmp_path_factory, answers_b_mixed) -> Path:
    """Fixture B's answers with one optional module on, so the mixed arms of the
    conditionals get rendered and asserted."""
    scratch = tmp_path_factory.mktemp("render-b-mixed")
    derived = scratch / "answers-unlike-mixed.yml"
    derived.write_text(yaml.safe_dump(answers_b_mixed, sort_keys=False))
    return render_cluster.render(scratch, answers=derived, dest_name="render-b-mixed")


@dataclass(frozen=True)
class Cluster:
    """One rendered repository plus the answers that produced it."""

    label: str
    path: Path
    answers: dict


# Everything not about the difference between the fixtures runs against both:
# fixture B is the only render reaching the modules-off branches, the larger
# roster, the single resolver and the unlike LAN.
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
            except (UnicodeDecodeError, OSError) as exc:
                UNREADABLE.append((str(path), exc.__class__.__name__))
                continue


def _k8s_files(root: Path, suffixes: tuple[str, ...] = ("*.yaml",)):
    k8s = root / "kubernetes"
    if not k8s.is_dir():
        return
    for pattern in suffixes:
        for path in sorted(k8s.rglob(pattern)):
            if path.is_file():
                yield path, path.read_text()


def _k8s_manifest_and_asset_files(root: Path):
    """Manifests plus the dashboard JSON, which is where a hostname leaks."""
    yield from _k8s_files(root, ("*.yaml", "*.yml", "*.json"))


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


# Dotfiles the generated repository is unusable without: the lint profiles its
# pre-commit hooks and pipeline select, and the attributes git applies. A `.git*`
# entry in copier.yml's wholesale `_exclude` would drop three of them unnoticed.
MUST_SHIP_DOTFILES = (
    ".copier-answers.yml",
    ".gitlab-ci.yml",
    ".gitattributes",
    ".gitignore",
    ".gitleaks.toml",
    ".editorconfig",
    ".ansible-lint",
    ".pre-commit-config.yaml",
    "ruff.toml",
    "lint/yamllint-relaxed.yml",
)


def test_render_produces_a_repository(cluster):
    assert (cluster.path / ".copier-answers.yml").is_file(), (
        "no answers file — copier update would not work"
    )
    missing = [name for name in MUST_SHIP_DOTFILES if not (cluster.path / name).is_file()]
    assert not missing, f"the render ships none of {missing}"


def test_answers_file_records_the_fixture(cluster):
    recorded = yaml.safe_load((cluster.path / ".copier-answers.yml").read_text())
    assert recorded["cluster_name"] == cluster.answers["cluster_name"]
    # lib_ref comes from copier.yml's default, so copier records the resolved
    # value and `copier update` reproduces it.
    assert recorded.get("lib_ref"), "the answers file must record the resolved lib_ref"


# Rendered lines that legitimately spell `{%`: a gate's own Jinja-detection
# literals and a Python %-format placeholder, not unrendered template text.
# Exact lines, never whole files - a file exemption hides a lost .jinja suffix.
_JINJA_SCAN_EXEMPT: set[tuple[str, str]] = {
    (
        "scripts/check-comment-length.py",
        "# place, `{% if x %}ci.yml{% endif %}` reads as the suffix `.yml{% endif %}`.",
    ),
    ("scripts/check-comment-length.py", 'JINJA_STATEMENT = re.compile(r"\\{%.*?%\\}", re.S)'),
    ("scripts/check-comment-length.py", '`{% if x %}ci.yml{% endif %}.jinja` -> `ci.yml`."""'),
    (
        "scripts/check-comment-length.py",
        "# A `{% raw %}` body is literal text: jinja reads no tags inside it, and an",
    ),
    (
        "scripts/check-comment-length.py",
        'r"(?P<open>\\{%-?\\s*raw\\s*-?%\\})(?P<body>.*?)(?P<close>\\{%-?\\s*endraw\\s*-?%\\})",',
    ),
    (
        "scripts/check-comment-length.py",
        "# (`{% %}`, the secrets seam a *.sh.jinja opens with) precede it.",
    ),
    (
        "scripts/check-comment-length.py",
        "A `{% raw %}` body is kept verbatim, only its tags dropped: jinja reads no",
    ),
    ("tests/test_validate_helm_values.py", "limits: {%s}"),
    (
        "scripts/check-role-inputs.py",
        '"{% if " + condition + " %}yes{% else %}no{% endif %}"',
    ),
}

# Rendered lines that legitimately spell `{{ <answer name> }}`: Ansible-level
# expressions the rendered inventory carries, whose variable names collide with
# copier answers. Exact lines, never whole files.
_ANSWER_SCAN_EXEMPT: set[tuple[str, str]] = {
    (
        "scripts/check-tailnet-dns-parity.py",
        "# `label.{{ internal_domain }}` in the Ansible rewrites.",
    ),
    ("tests/test_check_tailnet_dns_parity.py", '- domain: "dns.{{ internal_domain }}"'),
    ("tests/test_check_tailnet_dns_parity.py", '- domain: "k3s.{{ internal_domain }}"'),
    ("tests/test_check_tailnet_dns_parity.py", '- domain: "grafana.{{ internal_domain }}"'),
    ("tests/test_check_tailnet_dns_parity.py", 'answer: "{{ k3s_api_vip }}"'),
    ("tests/test_check_tailnet_dns_parity.py", 'answer: "{{ metallb_internal_vip }}"'),
    (
        "tests/test_inventory_single_source.py",
        '\'proxmox_lxc_gateway: "{{ lan_gateway }}"\\n\'',
    ),
}


def test_no_unrendered_jinja_statements(cluster):
    """A `{% ... %}` block in the output means a templated file was not given
    the .jinja suffix. Only `ansible/` is exempt, where playbooks embed Jinja by
    design; go-task, Grafana and Prometheus own `{{ }}`, not `{% %}`.
    """
    root = cluster.path
    leftovers = [
        f"{path.relative_to(root)}:{lineno}"
        for path, text in _text_files(root)
        if path.relative_to(root).parts[0] != "ansible"
        for lineno, line in enumerate(text.splitlines(), 1)
        if "{%" in line
        and (str(path.relative_to(root)), line.strip()) not in _JINJA_SCAN_EXEMPT
    ]
    assert not leftovers, "unrendered Jinja survived the render:\n  " + "\n  ".join(leftovers)


def test_every_text_file_was_read(cluster):
    """A file the walk cannot decode silently leaves every leakage scan below,
    which reads as a pass."""
    _ = list(_text_files(cluster.path))
    assert not UNREADABLE, "files dropped out of the leakage scans: " + ", ".join(
        f"{path} ({exc})" for path, exc in UNREADABLE
    )


def test_no_answer_survives_as_an_unrendered_expression(cluster):
    """`{{ <question> }}` in the output is a copier answer that was never
    substituted, most often a line inside a `{% raw %}` block. go-task parses
    the leftover as a function call and every task in the file dies.
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
        if match and (str(path.relative_to(root)), line.strip()) not in _ANSWER_SCAN_EXEMPT
    ]
    assert not offenders, (
        "copier answers left unsubstituted in the render:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# No reference-cluster values
# --------------------------------------------------------------------------


def _answered_literals(answers: dict) -> set[str]:
    """Values the answers spell, plus their dot- and slash-separated parts.

    lib_url and lib_project are left out: they name the upstream library, whose
    lines UPSTREAM_REPOS already exempts.
    """
    parts = set()
    for name, value in answers.items():
        if name in {"lib_url", "lib_project"} or not isinstance(value, str):
            continue
        parts.add(value)
        parts.update(piece for piece in re.split(r"[./:@\s]+", value) if piece)
    return parts


def test_no_reference_cluster_literals(cluster):
    answered = _answered_literals(cluster.answers)
    offenders = []
    for path, text in _text_files(cluster.path):
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(repo in line for repo in UPSTREAM_REPOS):
                continue
            where = f"{path.relative_to(cluster.path)}:{lineno}"
            for needle in FORBIDDEN_DOMAINS:
                if needle in line:
                    offenders.append(f"{where} {needle}")
            for pattern in FORBIDDEN_PATTERNS:
                for match in pattern.finditer(line):
                    if match.group(0) not in answered:
                        offenders.append(f"{where} {match.group(0)}")
    assert not offenders, (
        "reference-cluster identity leaked into the render — parameterize it:\n  "
        + "\n  ".join(offenders)
        + "\n  arms checked: "
        + ", ".join(FORBIDDEN_DOMAINS + tuple(p.pattern for p in FORBIDDEN_PATTERNS))
    )


# Every arm of the gate above, with text it must catch and text it must not.
# A dropped or inverted arm fails here instead of passing the render clean.
LITERAL_ARM_SAMPLES = [
    ("dns upstream 10.0.10.150", "dns upstream 10.77.4.3"),
    ("delegate_to: pve-opt-02", "delegate_to: pve-node-02"),
    ("remote_user: eric", "remote_user: ericsweiss"),
    ("issuer https://auth.ericsweiss.com", "issuer https://auth.example.com"),
    ("rewrite *.esweiss.com", "rewrite *.lan.example.com"),
]


@pytest.mark.parametrize("leak,clean", LITERAL_ARM_SAMPLES, ids=[s[0] for s in LITERAL_ARM_SAMPLES])
def test_every_reference_literal_arm_can_fail(leak, clean):
    def hits(line: str) -> list[str]:
        found = [needle for needle in FORBIDDEN_DOMAINS if needle in line]
        return found + [m.group(0) for p in FORBIDDEN_PATTERNS for m in p.finditer(line)]

    assert hits(leak), f"no arm caught {leak!r}"
    assert not hits(clean), f"an arm flagged {clean!r}, which names no reference value"


def test_the_two_fixtures_answer_differently(answers, answers_b):
    """The contrast fixture only proves anything while its answers differ. A
    key that drifts back into agreement silently disarms the leak check below."""
    assert set(answers) == set(answers_b), "the two fixtures answer different question sets"
    shared = {k for k, v in answers.items() if answers_b[k] == v}
    # The library URL and project path are the same upstream on purpose, and the
    # four backend seams have one implemented value each — a fixture answering
    # anything else is rejected by the question's own validator.
    assert shared <= {"lib_url", "lib_project", "git_backend", "secrets_backend",
                      "storage_backend", "dns_backend"}, (
        "answers that must differ between the fixtures now coincide: "
        + ", ".join(sorted(shared))
    )


# Answers whose fixture-A value cannot serve as evidence in a cross-render diff:
# an ordinary word, path component or example string proves nothing when found
# in render B. Each names its targeted gate below. Keep this list short.
CROSS_RENDER_EXEMPT = {
    "ci_runner_tag": (
        "'infrastructure' is also a path component (kubernetes/infrastructure/) and "
        "ordinary prose — test_every_job_carries_a_runner_tag is the targeted gate, "
        "and it takes the parameterized `cluster` fixture, so it runs on BOTH "
        "renders: a hardcoded tags: [\"infrastructure\"] fails on the unlike render"
    ),
    "external_domain": "'example.com' is RFC 2606's example domain, used in generic samples",
    "gpu": "'nvidia' is a vendor name that appears wherever the option is described",
    "node_exporter_job_regex": "'node-exporter' is the upstream exporter's own name",
    "onepassword_vault": "'Homelab' appears in a vendored library script's own docstring",
    "license_year": (
        "a four-digit year collides with every pinned version and copyright line; "
        "it reaches only LICENSE, which test_the_license_answer_decides_the_file holds"
    ),
}


# Every render built on fixture B: the modules-off fixture as shipped, the same
# answers with the optional modules on, and with one of them on.
@pytest.fixture(scope="session", params=["unlike", "unlike-modules-on", "unlike-mixed"])
def cluster_b(request) -> Cluster:
    render_fixture, answers_fixture = {
        "unlike": ("rendered_b", "answers_b"),
        "unlike-modules-on": ("rendered_b_modules_on", "answers_b_modules_on"),
        "unlike-mixed": ("rendered_b_mixed", "answers_b_mixed"),
    }[request.param]
    return Cluster(
        request.param,
        request.getfixturevalue(render_fixture),
        request.getfixturevalue(answers_fixture),
    )


def test_render_b_carries_no_fixture_a_values(cluster_b, answers):
    """No answer from fixture A appears in the non-prose files of a render from
    fixture B. Markdown is excluded, where a worked example may show one."""
    scoped = [
        (path, text) for path, text in _text_files(cluster_b.path) if path.suffix != ".md"
    ]
    leaks = []
    for key, value in answers.items():
        if key in CROSS_RENDER_EXEMPT:
            continue
        # Non-string answers are scanned by the gates that hold each one to
        # where it must appear: the conditional-module, CI-sizing, image-gc and
        # compose-guest tests in this module.
        if not isinstance(value, str) or value == cluster_b.answers.get(key) or len(value) < 4:
            continue
        # upstream_dns_servers and friends are space-separated lists.
        for token in value.split():
            if len(token) < 4:
                continue
            for path, text in scoped:
                if token in text:
                    leaks.append(f"{path.relative_to(cluster_b.path)}: {key}={token}")
    assert not leaks, (
        f"fixture A's answers appear in the {cluster_b.label} render — those values "
        "are hardcoded, not substituted:\n  " + "\n  ".join(sorted(set(leaks)))
    )


def test_the_image_gc_threshold_answer_reaches_the_kubelet_and_the_alert(cluster):
    """The image-gc threshold answer reaches both the kubelet flag and the alert.

    The one numeric answer the cross-render leak scan cannot see. The alert sits
    5 points above the flag, capped at 79 so DiskUsageWarning never inhibits it.
    """
    answer = int(cluster.answers["k3s_image_gc_high_threshold"])
    group_vars = yaml.safe_load(
        (cluster.path / "ansible/inventories/prod/group_vars/k3s.yml").read_text()
    )
    assert f"image-gc-high-threshold={answer}" in (group_vars.get("k3s_kubelet_args") or []), (
        "the k3s_image_gc_high_threshold answer does not reach k3s_kubelet_args, so "
        "the cluster prunes at the kubelet default while its alert is tuned to the answer"
    )
    release = (
        cluster.path / "kubernetes/infrastructure/observability/kube-prometheus-stack/release.yaml"
    ).read_text()
    assert f"> {min(answer + 5, 79)}\n" in release, (
        f"KubeletImageGCIneffective is not thresholded at min({answer} + 5, 79)"
    )
    assert f"of {answer}%: image GC is running" in release, (
        "the alert description names a threshold other than the answer"
    )


# --------------------------------------------------------------------------
# Kubernetes: substitution, not literals
# --------------------------------------------------------------------------


def test_cluster_config_holds_the_site_values(cluster):
    _, data = _cluster_config(cluster.path)
    assert data.get("cluster_internal_domain") == cluster.answers["internal_domain"]
    assert data.get("cluster_external_domain") == cluster.answers["external_domain"]
    assert data.get("cluster_k3s_api_vip") == cluster.answers["k3s_api_vip"]


# The kube-prometheus-stack subchart's DaemonSet job, spelled here because Helm
# renders its ServiceMonitor outside this corpus. Tied below to the
# `nodeExporter.enabled` switch that ships it.
CHART_NODE_EXPORTER_JOB = "node-exporter"


def _node_exporter_jobs(root: Path) -> set[str]:
    """The node-exporter Prometheus jobs the RENDER actually produces.

    Derived, never listed: a ServiceMonitor's `jobLabel` names the Service label
    whose value Prometheus uses as the `job` label on every series it scrapes.
    """
    observability = root / "kubernetes" / "infrastructure" / "observability"
    docs: list[dict] = []
    for path in sorted(observability.rglob("*.yaml")):
        docs += [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]

    services = [d for d in docs if d.get("kind") == "Service"]
    jobs: set[str] = set()
    for monitor in (d for d in docs if d.get("kind") == "ServiceMonitor"):
        spec = monitor.get("spec") or {}
        job_label = spec.get("jobLabel")
        selector = (spec.get("selector") or {}).get("matchLabels") or {}
        if not selector:
            continue
        for service in services:
            labels = (service.get("metadata") or {}).get("labels") or {}
            if all(labels.get(k) == v for k, v in selector.items()):
                # prometheus-operator's own resolution: the jobLabel's value,
                # and the Service name when the label is absent or unset.
                jobs.add(
                    labels.get(job_label) or (service.get("metadata") or {}).get("name")
                )

    release = yaml.safe_load(
        (observability / "kube-prometheus-stack" / "release.yaml").read_text()
    )
    if ((release["spec"]["values"] or {}).get("nodeExporter") or {}).get("enabled"):
        jobs.add(CHART_NODE_EXPORTER_JOB)

    # Only the node exporters: the corpus also ships unbound and zfs monitors,
    # which the node/storage alert scoping is not about.
    return {job for job in jobs if job and "node-exporter" in job}


def test_shipped_exporter_jobs_match_the_rendered_manifests(cluster):
    """`SHIPPED_EXPORTER_JOBS` is what makes the `node_exporter_job_regex`
    validator enforce something real, so it is checked against the render rather
    than trusted as a literal.
    """
    shipped = _node_exporter_jobs(cluster.path)
    assert shipped == set(SHIPPED_EXPORTER_JOBS), (
        f"the render ships node-exporter jobs {sorted(shipped)}, but "
        f"SHIPPED_EXPORTER_JOBS says {sorted(SHIPPED_EXPORTER_JOBS)}"
    )
    assert len(shipped) == len(SHIPPED_EXPORTER_JOBS), "SHIPPED_EXPORTER_JOBS has a duplicate"


def test_the_job_regex_in_cluster_config_covers_every_shipped_job(cluster):
    """The end of the same chain: the alert rules scope on
    `job=~"${cluster_node_exporter_job_regex}"`, so a shipped job the answered
    regex does not name is one whose alerts silently match zero series."""
    _, data = _cluster_config(cluster.path)
    named = set(data["cluster_node_exporter_job_regex"].split("|"))
    shipped = _node_exporter_jobs(cluster.path)
    assert shipped <= named, (
        f"jobs the render ships but the alert scoping omits: {sorted(shipped - named)}"
    )


def test_tailnet_gating_is_all_or_nothing(cluster, rendered, rendered_b):
    """`cluster_tailnet_cidr` and its consumers are gated on the same answer.
    Flux substitutes an undefined variable to an empty string, and an empty
    `ipAllowList.sourceRange` entry fails every internal route closed."""
    k8s = cluster.path / "kubernetes"
    users = sorted(
        str(path.relative_to(cluster.path))
        for path in k8s.rglob("*.yaml")
        if "cluster_tailnet_cidr" in path.read_text(encoding="utf-8")
    )
    _, data = _cluster_config(cluster.path)
    declared = "cluster_tailnet_cidr" in data
    if cluster.answers["vpn_tailscale"]:
        assert declared and users, (
            "the Tailscale module is on but the render declares or reads no "
            f"tailnet CIDR (declared={declared}, readers={users})"
        )
    else:
        assert not declared and not users, (
            "the Tailscale module is off but the render still carries "
            f"cluster_tailnet_cidr (declared={declared}, readers={users})"
        )


def test_compose_app_collection_is_all_or_nothing(cluster):
    """The collect_compose_app definition and its run_section call sites are gated
    on the same answer. A call site with no definition is a bash script that
    shellcheck and `bash -n` both accept and `task collect-state` fails on."""
    script = (cluster.path / "scripts/collect-state.sh").read_text(encoding="utf-8")
    guests = cluster.answers["compose_app_guests"] or []
    defined = "collect_compose_app() {" in script
    calls = [
        line
        for line in script.splitlines()
        if "collect_compose_app" in line and "run_section" in line
    ]
    if guests:
        assert defined, (
            "compose_app_guests are answered but the collector defines no "
            "collect_compose_app"
        )
        assert len(calls) == len(guests), (
            f"{len(guests)} compose guests answered but {len(calls)} run_section "
            "call sites"
        )
        for guest in guests:
            expected = f'run_section "{guest["label"]}:{guest["host"]}" collect_compose_app'
            assert any(expected in line for line in calls), f"no call site for {expected}"
    else:
        assert not defined and not calls, (
            "compose_app_guests is empty but the collector still carries "
            "collect_compose_app"
        )


# A path whose directory or file name is a copier conditional: the render
# carries it only under answers that satisfy the condition. `tail` is the
# literal suffix outside it, as in `{% if x %}LICENSE{% endif %}.jinja`.
_CONDITIONAL_NAME = re.compile(
    r"\{%-?\s*if\s+(?P<cond>.+?)\s*-?%\}(?P<name>[^{}]+?)\{%-?\s*endif\s*-?%\}"
    r"(?P<tail>[^{}]*)"
)

# The render's own suites build paths as negative-case fixtures, so a name
# inside one is test data rather than somewhere an operator is sent.
_FIXTURE_FILE = re.compile(r"(^|/)test_[^/]+\.py$")


def _conditional_path_conditions() -> dict[str, set[str]]:
    """Render-relative path -> every condition its own name components carry."""
    template_root = TEMPLATE_ROOT
    paths: dict[str, set[str]] = {}
    for path in sorted(template_root.rglob("*")):
        rel = path.relative_to(template_root)
        if not any("{%" in part for part in rel.parts):
            continue
        parts, conditions, unresolved = [], set(), False
        for part in rel.parts:
            match = _CONDITIONAL_NAME.fullmatch(part)
            if match:
                parts.append(match.group("name") + match.group("tail"))
                conditions.add(match.group("cond"))
            elif "{%" in part:
                # A conditional spanning only part of a name (a loop, or a value
                # interpolated beside one): the rendered name is not literal.
                unresolved = True
            else:
                parts.append(part)
        if not unresolved:
            rendered = "/".join(parts).removesuffix(TEMPLATES_SUFFIX)
            paths.setdefault(rendered, set()).update(conditions)
    return paths


def _conditional_paths() -> list[str]:
    """Every render-relative path the template ships behind a conditional."""
    return sorted(_conditional_path_conditions())


def _condition_holds(condition: str, answers: dict) -> bool:
    """Evaluate one copier path condition against an answer set.

    StrictUndefined, so a condition naming an answer the fixture never gives
    fails loudly instead of reading as false and expecting the path to be absent.
    """
    env = copier_env(undefined=jinja2.StrictUndefined)
    return env.from_string(f"{{% if {condition} %}}1{{% endif %}}").render(**answers) == "1"


def _assert_conditional_paths_follow_their_answers(cluster: Cluster) -> None:
    """Every conditionally-named path is present exactly when its answers say so."""
    conditions = _conditional_path_conditions()
    assert conditions, "no conditional path found under the template — this gate walked nothing"
    wrong = []
    for rel, conds in sorted(conditions.items()):
        expected = all(_condition_holds(cond, cluster.answers) for cond in sorted(conds))
        if expected != (cluster.path / rel).exists():
            verb = "excluded it" if expected else "shipped it"
            wrong.append(f"{rel} (gated on {' and '.join(sorted(conds))}): the render {verb}")
    assert not wrong, (
        f"the {cluster.label} render disagrees with its own answers:\n  " + "\n  ".join(wrong)
    )


# Every hook the generated repository's pre-commit config must carry. Before
# `flux bootstrap` and the first pipeline run they are the only automatic run
# of the secret scan and yamllint a new cluster gets.
PRE_COMMIT_HOOKS = {
    "gitleaks",
    "yamllint",
    "end-of-file-fixer",
    "trailing-whitespace",
    "check-merge-conflict",
    "check-yaml",
}


def test_every_pre_commit_hook_ships(cluster):
    config = cluster.path / ".pre-commit-config.yaml"
    assert config.is_file(), "the render ships no .pre-commit-config.yaml"
    doc = yaml.safe_load(config.read_text()) or {}
    repos = doc.get("repos") or []
    hooks = {hook["id"] for repo in repos for hook in repo.get("hooks") or []}
    assert PRE_COMMIT_HOOKS <= hooks, (
        "the generated repository's pre-commit config dropped "
        f"{sorted(PRE_COMMIT_HOOKS - hooks)} — on a cluster whose pipeline is not "
        "yet wired the hook is the only automatic run of that gate"
    )
    # `repo: local` runs the repository's own gates and takes no rev.
    unpinned = [
        repo["repo"]
        for repo in repos
        if repo.get("repo") != "local" and not str(repo.get("rev", "")).startswith("v")
    ]
    assert not unpinned, f"these pre-commit repos are not pinned to a tag: {unpinned}"
    profiles = [
        Path(args[index + 1])
        for repo in repos
        for hook in repo.get("hooks") or []
        if (args := [str(a) for a in hook.get("args") or []])
        for index, arg in enumerate(args)
        if arg == "-c"
    ]
    assert profiles, "no pre-commit hook selects a lint profile with -c"
    for profile in profiles:
        assert (cluster.path / profile).is_file(), (
            f"a pre-commit hook selects {profile}, which the render does not ship: "
            "the hook fails on every commit in a fresh clone"
        )


def test_every_local_hook_brings_the_dependencies_its_script_imports(cluster):
    """Every `repo: local` hook declares the dependencies its script imports.

    `language: system` runs the hook against the operator's own python3, so only
    a stdlib-only or shell gate may use it; the rest pin deps in their own venv.
    """
    config = yaml.safe_load((cluster.path / ".pre-commit-config.yaml").read_text()) or {}
    hooks = [
        hook
        for repo in config.get("repos") or []
        if repo.get("repo") == "local"
        for hook in repo.get("hooks") or []
    ]
    assert hooks, "the render ships no `repo: local` pre-commit hooks"
    offenders = []
    for hook in hooks:
        scripts = [
            cluster.path / token
            for token in str(hook.get("entry", "")).split()
            if token.startswith("scripts/")
        ]
        imports_yaml = any(
            path.is_file() and re.search(r"^\s*import yaml\b", path.read_text(), re.M)
            for path in scripts
        )
        pinned = any("pyyaml" in str(dep).lower() for dep in hook.get("additional_dependencies") or [])
        if imports_yaml and not (hook.get("language") == "python" and pinned):
            offenders.append(f"{hook['id']}: language={hook.get('language')!r}")
    assert not offenders, (
        "these pre-commit hooks import PyYAML without bringing it into their own "
        "hook venv, so they die on a host python3 that lacks it:\n  "
        + "\n  ".join(offenders)
    )


def test_every_rendered_text_file_ends_with_exactly_one_newline(cluster):
    """The render satisfies the `end-of-file-fixer` hook it ships.

    A template whose final jinja tag emits its own newline renders a trailing
    blank line, so the operator's first commit is rewritten by their own hook.
    """
    offenders = []
    for path, text in _text_files(cluster.path):
        if "__pycache__" in path.parts:
            continue
        if not text:
            continue
        if not text.endswith("\n"):
            offenders.append(f"{path.relative_to(cluster.path)}: no final newline")
        elif text.endswith("\n\n"):
            offenders.append(f"{path.relative_to(cluster.path)}: blank line at EOF")
    assert not offenders, (
        "end-of-file-fixer would rewrite these rendered files:\n  "
        + "\n  ".join(offenders)
    )


def test_nothing_points_at_a_path_the_answers_excluded(cluster):
    """Nothing names a file the answers excluded.

    Such a reference is dead on a cluster that left the component out: Flux
    fails the Kustomization and the operator reads a path that does not exist.
    """
    conditional = _conditional_paths()
    assert conditional, "no conditional path found under the template — this gate walked nothing"
    excluded = [rel for rel in conditional if not (cluster.path / rel).exists()]
    if cluster.label == "unlike":
        assert excluded, (
            "the modules-off fixture excluded no conditional path — it no longer "
            "covers the branches the shaped render never takes"
        )
    offenders = []
    for path, text in _text_files(cluster.path):
        rel_self = str(path.relative_to(cluster.path))
        if _FIXTURE_FILE.search(rel_self):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for rel in excluded:
                if rel in line:
                    offenders.append(f"{rel_self}:{lineno} names {rel}")
    assert not offenders, (
        "these lines name a path this render's answers excluded:\n  "
        + "\n  ".join(offenders)
    )


def test_every_conditional_path_follows_its_own_answer(cluster):
    _assert_conditional_paths_follow_their_answers(cluster)


def test_every_conditional_path_follows_its_own_answer_on_fixture_b(cluster_b):
    """Including the mixed render: a path gated on one optional module must not
    appear or vanish with another module's answer."""
    _assert_conditional_paths_follow_their_answers(cluster_b)


# One entry point per optional module, keyed by the exact condition its path
# name must carry. The derived gate above proves a conditional path follows its
# answer; it cannot see a module whose conditional was dropped altogether.
GATED_ENTRY_POINTS = {
    "use_unifi": (
        "terraform/unifi/main.tf",
        "docs/unifi-network.md",
        "scripts/check-unifi-doc-parity.py",
    ),
    "vpn_tailscale": (
        "kubernetes/apps/tailnet-dns/kustomization.yaml",
        "kubernetes/infrastructure/controllers/tailscale-operator/release.yaml",
        "terraform/tailscale/main.tf",
    ),
    "gpu == 'nvidia'": (
        "kubernetes/infrastructure/controllers/nvidia-device-plugin/release.yaml",
        "kubernetes/infrastructure/sources/nvidia.yaml",
    ),
    "git_backend == 'gitlab_selfhosted'": (
        "kubernetes/apps/gitlab-runner/release.yaml",
        "kubernetes/infrastructure/sources/gitlab.yaml",
    ),
    "secrets_backend == 'onepassword'": (
        "kubernetes/infrastructure/configs/cluster-secret-store.yaml",
        "kubernetes/infrastructure/controllers/onepassword-connect/release.yaml",
    ),
    "dns_backend == 'cloudflare'": (
        "kubernetes/infrastructure/configs/cloudflare-ddns/cronjob.yaml",
        "kubernetes/infrastructure/configs/shared-dns-secrets/dns-api-token.yaml",
    ),
    "license == 'mit'": ("LICENSE",),
    "storage_backend == 'zfs'": (
        "kubernetes/infrastructure/observability/exporters/zfs-exporter.yaml",
        "ansible/integration-tests/storage-stack/molecule/default/converge.yml",
    ),
}


def test_the_license_answer_decides_the_file(cluster):
    """`license: none` must leave the repository unlicensed, and `mit` must name
    the holder — a LICENSE naming the wrong party is worse than none."""
    license_file = cluster.path / "LICENSE"
    readme = (cluster.path / "README.md").read_text(encoding="utf-8")
    if cluster.answers["license"] == "mit":
        assert license_file.is_file(), "license is mit but no LICENSE was rendered"
        text = license_file.read_text(encoding="utf-8")
        assert cluster.answers["license_holder"] in text
        assert cluster.answers["license_year"] in text
        assert "## License" in readme
    else:
        assert not license_file.exists(), "license is none but a LICENSE was rendered"
        assert "## License" not in readme


def test_every_optional_module_keeps_its_conditional():
    """A dropped or retargeted path conditional ships an optional module to a
    cluster that cannot use it, which renders green and reconciles into a
    Kustomization nothing satisfies."""
    conditions = _conditional_path_conditions()
    wrong = []
    for condition, paths in sorted(GATED_ENTRY_POINTS.items()):
        for rel in paths:
            carried = conditions.get(rel)
            if carried is None:
                wrong.append(f"{rel} ships unconditionally; gate it on {condition}")
            elif condition not in carried:
                wrong.append(f"{rel} is gated on {sorted(carried)}, not {condition}")
    assert not wrong, "optional modules whose conditional moved:\n  " + "\n  ".join(wrong)
    declared = set().union(*conditions.values())
    assert declared == set(GATED_ENTRY_POINTS), (
        "path conditions no entry point holds: "
        f"{sorted(declared - set(GATED_ENTRY_POINTS))}; named but unused: "
        f"{sorted(set(GATED_ENTRY_POINTS) - declared)}"
    )


def test_rw_nfs_exports_are_scoped_to_named_hosts(cluster):
    """A writable export open to the whole LAN is the one NFS mistake that costs
    data, so every rw client spec is a single host."""
    nas = cluster.path / "ansible" / "inventories" / "prod" / "group_vars" / "nas.yml"
    assert nas.is_file(), (
        "the render ships no ansible/inventories/prod/group_vars/nas.yml — this "
        "gate ran nothing"
    )
    exports = yaml.safe_load(nas.read_text()).get("nas_storage_exports") or []
    checked, offenders = 0, []
    for export in exports:
        for client in export.get("clients") or []:
            options = client.get("options") or ""
            if "rw" not in options.split(","):
                continue
            checked += 1
            if not str(client.get("spec", "")).endswith("/32"):
                offenders.append(f"{export.get('path')} -> {client.get('spec')}")
    assert checked, "no writable export was examined — the inventory shape moved"
    assert not offenders, (
        "writable NFS exports that are not scoped to one host:\n  " + "\n  ".join(offenders)
    )


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
    """The hard-coded-domain problem this template exists to avoid."""
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
    for path, text in _k8s_manifest_and_asset_files(cluster.path):
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


# Certificates issued as an anchor copy rather than for a route of their own:
# `default` holds no IngressRoute, and every namespace that serves a wildcard
# keeps its own copy staggered off these two.
CERTIFICATE_ANCHOR_NAMESPACES = {"default"}

_ROUTE_HOST = re.compile(r"Host\(`([^`]+)`\)")


def _substituted(value: str, config: dict[str, str]) -> str:
    """A manifest string with its ${cluster_*} placeholders resolved, the way
    Flux substitutes them at reconcile time."""
    for key, replacement in config.items():
        value = value.replace("${" + key + "}", replacement)
    return value


def _tls_identifiers(root: Path) -> tuple[dict, dict]:
    """(namespace, secretName) -> issued dnsNames, and -> hostnames served."""
    _, config = _cluster_config(root)
    certificates: dict[tuple[str, str], set[str]] = {}
    routes: dict[tuple[str, str], set[str]] = {}
    for _, raw in _k8s_files(root, ("*.yaml", "*.yml")):
        for doc in yaml.safe_load_all(raw):
            if not isinstance(doc, dict):
                continue
            namespace = (doc.get("metadata") or {}).get("namespace")
            spec = doc.get("spec") or {}
            if doc.get("kind") == "Certificate":
                key = (namespace, spec.get("secretName"))
                certificates.setdefault(key, set()).update(
                    _substituted(str(name), config) for name in spec.get("dnsNames") or []
                )
            elif doc.get("kind") == "IngressRoute":
                secret = (spec.get("tls") or {}).get("secretName")
                if not secret:
                    continue
                hosts = {
                    _substituted(host, config)
                    for route in spec.get("routes") or []
                    for host in _ROUTE_HOST.findall(str(route.get("match", "")))
                }
                routes.setdefault((namespace, secret), set()).update(hosts)
    return certificates, routes


def _issued_for(hostname: str, dns_names: set[str]) -> bool:
    """One identifier, matched against a cert's dnsNames. A wildcard covers
    exactly one label, as the ACME specification has it."""
    parent = hostname.split(".", 1)[-1]
    return hostname in dns_names or f"*.{parent}" in dns_names


def test_every_ingress_route_is_served_by_a_certificate(cluster):
    """Every IngressRoute's `tls.secretName` is issued by a Certificate.

    An unissued one serves Traefik's self-signed default: a browser warning, not
    a red gate. Traefik reads Secrets only from the route's own namespace.
    """
    certificates, routes = _tls_identifiers(cluster.path)
    assert routes, "the render ships no IngressRoute with TLS — this gate examined nothing"
    problems = []
    for (namespace, secret), hosts in sorted(routes.items()):
        issued = certificates.get((namespace, secret))
        if issued is None:
            problems.append(f"{namespace}/{secret}: no Certificate in that namespace issues it")
            continue
        for host in sorted(hosts):
            if not _issued_for(host, issued):
                problems.append(
                    f"{namespace}/{secret}: serves {host}, absent from its dnsNames {sorted(issued)}"
                )
    assert not problems, "IngressRoutes without a usable certificate:\n  " + "\n  ".join(problems)


def test_every_certificate_is_claimed_by_a_route(cluster):
    """The other direction: an unreferenced Certificate still orders from the
    ACME endpoint and renews forever, spending the duplicate-certificate rate
    limit the staggered copies are tuned around."""
    certificates, routes = _tls_identifiers(cluster.path)
    assert certificates, "the render ships no Certificate — this gate examined nothing"
    unclaimed = sorted(
        f"{namespace}/{secret}"
        for (namespace, secret) in certificates
        if namespace not in CERTIFICATE_ANCHOR_NAMESPACES
        and (namespace, secret) not in routes
    )
    assert not unclaimed, (
        "Certificates no IngressRoute references, and not an anchor copy in "
        f"{sorted(CERTIFICATE_ANCHOR_NAMESPACES)}: {unclaimed}"
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
    assert req.is_file(), (
        "the render ships no ansible/requirements.yml — this gate ran nothing"
    )
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
    assert playbooks.is_dir(), (
        "the render ships no ansible/playbooks/ — this gate ran nothing"
    )
    bare = []
    # Both suffixes: a playbook written `.yaml` must not slip past this gate.
    for path in sorted(p for suffix in ("*.yml", "*.yaml") for p in playbooks.rglob(suffix)):
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
    """flux-lint renders through the local scripts/flux-env.sh wrapper, which
    passes both ConfigMaps. The library helper takes one per call, so pointing
    at it silently drops every cluster_* substitution."""
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
# site data they read. Each covers a defect class kubeconform structurally
# cannot see. See docs/CI.md.
EXTRA_VALIDATION_GATES = (
    "scripts/check-hpa-vpa-invariant.py",
    "scripts/check-scrape-netpol.py",
    "scripts/check-default-deny-coverage.py",
    "scripts/check-secretstore-scope.py",
    "scripts/check-pvc-storageclass.py",
    "scripts/check-backup-artifact-apps.py",
    "scripts/validate-helm-values.py",
    "scripts/autoscaling-policy.yaml",
    "scripts/helm-values-releases.yaml",
)


CORPUS_GATE_WRAPPER = "scripts/flux-corpus-gates.sh"


def test_flux_lint_runs_the_extra_validation_gates(cluster):
    """Every gate is wired AND present. A generated cluster gets the platform's
    architecture; without these it does not get the checks that keep it.

    extra_validation calls one wrapper, so the gate list is asserted inside it.
    """
    extra = _flux_lint_include(cluster.path)["inputs"].get("extra_validation", "")
    assert CORPUS_GATE_WRAPPER in extra, f"extra_validation does not run {CORPUS_GATE_WRAPPER}"
    wrapper = cluster.path / CORPUS_GATE_WRAPPER
    assert wrapper.is_file(), f"extra_validation references a missing {CORPUS_GATE_WRAPPER}"
    body = wrapper.read_text()
    for referenced in EXTRA_VALIDATION_GATES:
        assert referenced in body, f"{CORPUS_GATE_WRAPPER} does not run {referenced}"
        assert (cluster.path / referenced).is_file(), (
            f"{CORPUS_GATE_WRAPPER} references a missing {referenced}"
        )
    # `changes:` decides whether the job runs at all, so a gate not named there
    # is skipped by exactly the MR that loosens it.
    changes = _flux_lint_include(cluster.path)["inputs"].get("changes") or []
    for referenced in (*EXTRA_VALIDATION_GATES, CORPUS_GATE_WRAPPER):
        assert referenced in changes, (
            f"flux-lint's changes: does not name {referenced}, so an MR editing "
            "only that file never starts the job it weakens"
        )


def _taskfile_texts(root: Path) -> list[str]:
    """The Taskfile tree as text: the root file plus every namespace file."""
    paths = [root / "Taskfile.yml", *sorted((root / "taskfiles").glob("*.yml"))]
    return [path.read_text(encoding="utf-8") for path in paths if path.is_file()]


def _taskfile_tree(root: Path) -> dict:
    """Fully qualified task name -> task body, across the Taskfile tree.

    A task in taskfiles/<ns>.yml is addressed as `<ns>:<task>`, so the file stem
    is the namespace prefix.
    """
    tasks = dict((yaml.safe_load((root / "Taskfile.yml").read_text()) or {}).get("tasks") or {})
    for path in sorted((root / "taskfiles").glob("*.yml")):
        included = yaml.safe_load(path.read_text()) or {}
        for name, body in (included.get("tasks") or {}).items():
            tasks[f"{path.stem}:{name}"] = body
    return tasks


def test_task_lint_mirrors_the_ci_lint_stage(cluster):
    """`task lint` must run every gate the CI lint stage runs.

    Both sides call the corpus-gate wrapper, so the gate list itself cannot
    differ between them; what is asserted here is that neither drops it.
    """
    tasks = _taskfile_tree(cluster.path)
    flux_lint = "\n".join(str(step) for step in (tasks.get("flux:lint") or {}).get("cmds") or [])
    extra = _flux_lint_include(cluster.path)["inputs"].get("extra_validation", "")
    assert CORPUS_GATE_WRAPPER in extra, "the CI flux-lint job does not call the corpus gates"
    assert CORPUS_GATE_WRAPPER in flux_lint, "`task flux:lint` does not call the corpus gates"
    gates = set(re.findall(r"scripts/[\w.-]+\.py", (cluster.path / CORPUS_GATE_WRAPPER).read_text()))
    assert gates, f"no gate script parsed out of {CORPUS_GATE_WRAPPER}"
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


def test_flux_lint_renders_through_fluxs_strict_envsubst(cluster):
    """`task flux:lint` must run each build through `flux envsubst --strict`.

    GNU envsubst accepts forms Flux's Go envsubst rejects (`${conf%/*}`), so a
    gate without it passes a commit whose Kustomization fails its post-build.
    """
    tasks = _taskfile_tree(cluster.path)
    lint = tasks.get("flux:lint") or {}
    script = "\n".join(str(step) for step in lint.get("cmds") or [])
    assert script, "the rendered flux:lint has no cmds — this gate read nothing"
    assert "flux envsubst --strict" in script, (
        "the rendered flux:lint never runs Flux's strict substitution, so only "
        "the cheap pre-substitution scans stand between a bad placeholder and "
        "a BuildFailed Kustomization on main"
    )
    strict = script.index("flux envsubst --strict")
    gnu = script.index('envsubst "$FLUX_ENVSUBST_VARS"')
    assert strict < gnu, (
        "the strict pass runs after the GNU render, so a form Flux rejects is "
        "reported only once kubeconform has already validated the output"
    )
    preconditions = [
        str(entry.get("sh") if isinstance(entry, dict) else entry)
        for entry in lint.get("preconditions") or []
    ]
    assert "command -v flux" in preconditions, (
        "flux:lint runs `flux envsubst --strict` with no `command -v flux` "
        f"precondition, so a missing CLI fails mid-render: {preconditions}"
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


_NETPOL_GATE = "scripts/check-netpol-except-parity.py"


def _netpol_gate_argv(root: Path) -> dict[str, list[str]]:
    """The egress-fence invocation as each shipped runner spells it.

    The interpreter token is dropped: the pre-commit hook runs `python` inside
    its own venv, the Taskfile and the pipeline run `python3`.
    """
    tasks = _taskfile_tree(root)
    config = yaml.safe_load((root / ".pre-commit-config.yaml").read_text()) or {}
    sources = {
        "task lint:netpol-parity": [
            str(step) for step in (tasks.get("lint:netpol-parity") or {}).get("cmds") or []
        ],
        "the netpol-parity CI job": [
            str(step)
            for step in (_load_ci(root / ".gitlab-ci.yml").get("netpol-parity") or {}).get("script")
            or []
        ],
        "the netpol-except-parity pre-commit hook": [
            str(hook.get("entry", ""))
            for repo in config.get("repos") or []
            if repo.get("repo") == "local"
            for hook in repo.get("hooks") or []
        ],
    }
    argv = {}
    for name, lines in sources.items():
        matching = [shlex.split(line)[1:] for line in lines if _NETPOL_GATE in line]
        assert len(matching) == 1, (
            f"{name} invokes {_NETPOL_GATE} {len(matching)} times, expected once"
        )
        argv[name] = matching[0]
    return argv


def test_every_shipped_egress_fence_invocation_is_the_same_command(cluster):
    """The three runners are asserted by name elsewhere; a flag or path typo in
    any one of them is invisible to that. Held as one string so a changed flag
    has to move all three."""
    argv = _netpol_gate_argv(cluster.path)
    assert len({tuple(value) for value in argv.values()}) == 1, (
        "the egress-fence gate is spelled differently by its runners, so they do "
        f"not check the same thing: {argv}"
    )


def test_the_shipped_egress_fence_invocation_passes_in_the_render(cluster):
    """Runs the command the rendered Taskfile ships rather than this test's own
    spelling of it. The gate exits 2 when it scans nothing, so a path that no
    longer resolves fails here instead of in every generated cluster."""
    argv = _netpol_gate_argv(cluster.path)["task lint:netpol-parity"]
    result = subprocess.run(
        [sys.executable, *argv],
        cwd=cluster.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        " ".join(argv) + "\n" + result.stdout + result.stderr
    )


# Advisory jobs the gate deliberately leaves outside validation-gate.needs: each
# reds on a broken plan, runs only on a schedule or on main, and gates a
# supervised `terraform apply`, never a deploy job.
ADVISORY_UNGATED = {
    "authentik-drift-plan",
    "tailscale-drift-plan",
    "unifi-drift-plan",
}


def test_every_blocking_check_is_in_the_deploy_gate(cluster):
    """Every deploy job needs validation-gate and nothing else, so a lint,
    validate or test job outside its `needs:` never blocks a deploy. Only
    `allow_failure: true` exempts one: the `exit_codes:` form still reds a job."""
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    needs = {
        entry.get("job") if isinstance(entry, dict) else entry
        for entry in (ci.get("validation-gate") or {}).get("needs") or []
    }
    assert needs, "validation-gate declares no needs — this gate examined nothing"
    ungated = sorted(
        name
        for name, job in ci.items()
        if isinstance(job, dict)
        and not name.startswith(".")
        and job.get("stage") in {"lint", "validate", "test"}
        and job.get("allow_failure") is not True
        and name not in needs
        and name not in ADVISORY_UNGATED
    )
    assert not ungated, (
        "these blocking checks are not in validation-gate.needs, so a deploy "
        f"proceeds past them when they are red: {ungated}"
    )


# Top-level pipeline keys that are not jobs, including the job-keyword globals
# GitLab allows there. One read as a job name would make this gate demand a
# documentation row for it.
_CI_RESERVED = {
    "stages",
    "default",
    "workflow",
    "variables",
    "include",
    "image",
    "services",
    "cache",
    "before_script",
    "after_script",
}


def _local_jobs(ci: dict) -> set[str]:
    """Every job `.gitlab-ci.yml` defines itself — hidden fragments excluded."""
    return {
        name
        for name, job in ci.items()
        if isinstance(job, dict) and name not in _CI_RESERVED and not name.startswith(".")
    }


# Hidden fragments the LIBRARY includes define. A job may extend one of these
# without the pipeline declaring it; any other missing target is a typo, and
# GitLab refuses to create a pipeline that carries one.
LIB_CI_FRAGMENTS = frozenset(
    {".deploy-base", ".install-1password", ".terraform-http-backend"}
)


def _extends_targets(job: dict) -> list[str]:
    targets = job.get("extends") or []
    return [targets] if isinstance(targets, str) else list(targets)


def _resolved(ci: dict, name: str, seen: tuple[str, ...] = ()) -> dict:
    """One job with its `extends:` chain applied, the job's own keys winning.

    An allowlisted library fragment contributes nothing here, so a key that can
    only come from one reads as absent rather than as a resolution failure.
    """
    body = dict(ci[name])
    targets = _extends_targets(body)
    body.pop("extends", None)
    for target in targets:
        assert target.startswith("."), f"{name} extends {target}, which is not a hidden job"
        assert target not in seen, (
            "extends cycle: " + " -> ".join((*seen, name, target))
        )
        if target in LIB_CI_FRAGMENTS:
            continue
        assert target in ci, (
            f"{name} extends {target}, which neither this pipeline nor the "
            "library-fragment allowlist declares — GitLab creates no pipeline at all"
        )
        body = {**_resolved(ci, target, (*seen, name)), **body}
    return body


def test_every_extends_target_resolves(cluster):
    """A job extending a name nothing defines is not a red job: GitLab refuses
    the whole pipeline, so a generated cluster gets none. A library fragment is
    allowed by name, so a typo cannot hide behind "the library defines it"."""
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    extending = 0
    for name in [*sorted(_local_jobs(ci)), *(n for n in ci if n.startswith("."))]:
        if not isinstance(ci[name], dict):
            continue
        extending += bool(_extends_targets(ci[name]))
        stage = _resolved(ci, name).get("stage")
        assert stage is None or stage in ci["stages"], (
            f"{name} resolves to stage {stage!r}, which `stages:` does not declare"
        )
    assert extending, "no job extends anything — this gate examined nothing"
    unused = sorted(
        LIB_CI_FRAGMENTS
        - {t for n in ci if isinstance(ci[n], dict) for t in _extends_targets(ci[n])}
    )
    assert not unused, (
        f"LIB_CI_FRAGMENTS allows {unused}, which this render extends nowhere — a "
        "stale name here lets a typo resolve"
    )


def test_the_extends_walk_rejects_a_chain_it_cannot_resolve():
    """Mutation proof for the walk above, which the render exercises only on its
    happy path."""
    ci = {"stages": ["lint"], "a": {"extends": ".typo"}, ".b": {"extends": ".b"}}
    with pytest.raises(AssertionError, match="library-fragment allowlist"):
        _resolved(ci, "a")
    with pytest.raises(AssertionError, match="extends cycle"):
        _resolved(ci, ".b")
    with pytest.raises(AssertionError, match="not a hidden job"):
        _resolved({"a": {"extends": "b"}, "b": {}}, "a")
    # An allowlisted library fragment resolves to the job's own keys alone.
    assert _resolved({"a": {"extends": ".deploy-base", "stage": "deploy"}}, "a") == {
        "stage": "deploy"
    }


# Script tokens that name a path in the repository. A job's `changes:` has to
# cover each, or the MR that edits only that file never starts the job.
_SCRIPT_PATH_PREFIXES = (
    ".gitlab/", "ansible/", "kubernetes/", "lint/", "scripts/", "taskfiles/",
    "terraform/", "tests/",
)


def _changes_covers(token: str, patterns: list[str]) -> bool:
    """fnmatch the token against `changes:`, directory tokens included.

    A directory token needs stand-in files one and two levels down to match
    `dir/**/*`. GitLab's `**` spans zero segments, so patterns get a second spelling.
    """
    base = token.rstrip("/")
    spellings = (token, base, f"{base}/file.yaml", f"{base}/sub/file.yaml")
    readings = [
        reading
        for pattern in patterns
        for reading in ({pattern, pattern.replace("**/", "")})
    ]
    return any(fnmatch(spelling, reading) for reading in readings for spelling in spellings)


def test_every_job_reruns_on_the_paths_it_names(cluster):
    """A narrowed `changes:` must still name every path the job's script reads,
    or the job skips exactly the MR that weakens it."""
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    checked = 0
    for name in sorted(_local_jobs(ci)):
        body = _resolved(ci, name)
        rules = [rule for rule in body.get("rules") or [] if isinstance(rule, dict)]
        merge = [rule for rule in rules if "merge_request_event" in str(rule.get("if", ""))]
        if not merge:
            continue
        changes = [str(pattern) for pattern in merge[0].get("changes") or []]
        if not changes:
            continue
        script = " ".join(str(step) for step in body.get("script") or [])
        for raw in script.split():
            token = raw.strip("'\"();|&")
            if not token.startswith(_SCRIPT_PATH_PREFIXES):
                continue
            assert _changes_covers(token, changes), (
                f"{name} runs {token} but no `changes:` entry covers it, so an MR "
                "editing only that file never starts the job"
            )
            checked += 1
    assert checked, "no job names a path in its script — this gate examined nothing"


def test_the_changes_matcher_rejects_a_narrowed_list():
    """Mutation proof for the matcher above: narrowing a `changes:` list past a
    path the script reads must not keep matching."""
    assert _changes_covers("scripts/gate.py", ["scripts/**/*.py"])
    assert not _changes_covers("scripts/gate.py", ["ansible/**/*", ".gitlab-ci.yml"])
    # A directory token is covered by a pattern over its contents, at any depth.
    assert _changes_covers("kubernetes/", ["kubernetes/**/*"])
    assert not _changes_covers("kubernetes/", ["ansible/**/*"])


# Library-include inputs whose value is a path in the render. Register a new
# path-bearing input here, or nothing holds it to a directory that exists.
PATH_INPUTS = (
    "cluster_dir",
    "flux_render_script",
    "kustomize_path",
    "skipped_script",
    "targets",
    "versions_configmap",
)


def test_pipeline_inputs_name_only_paths_that_exist(cluster):
    """Every tool behind these inputs exits non-zero on a path that is not
    there: a cluster rename or a directory move otherwise ships a pipeline
    pointed at nothing, and fails first in the operator's own pipeline."""
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    checked = []
    for include in ci.get("include") or []:
        if not isinstance(include, dict):
            continue
        inputs = include.get("inputs") or {}
        for key in PATH_INPUTS:
            value = inputs.get(key)
            if not value:
                continue
            for target in str(value).split():
                if target == ".":
                    continue
                assert (cluster.path / target).exists(), (
                    f"{include.get('file')} input {key} names a missing {target}"
                )
                checked.append(target)
    assert checked, "no include declares a path input — this gate examined nothing"


def _changes_names(token: str, patterns: list[str]) -> bool:
    """True when some `changes:` glob shares a path lineage with this target.

    The suffix half is the tool's own: ruff walks `.py`, yamllint `.yml`. What
    has to match is the directory, in either direction.
    """
    target = PurePosixPath(token.rstrip("/"))
    for pattern in patterns:
        literal = pattern.split("*", 1)[0].rstrip("/")
        if not literal:
            return True
        candidate = PurePosixPath(literal)
        if target.is_relative_to(candidate) or candidate.is_relative_to(target):
            return True
    return False


def _assert_lint_targets_are_named_by_changes(ci: dict, label: str) -> None:
    uncovered, checked = [], 0
    for include in ci.get("include") or []:
        if not isinstance(include, dict):
            continue
        inputs = include.get("inputs") or {}
        targets = str(inputs.get("targets") or "").split()
        # `changes` is an include INPUT the job's rules read, not an include key.
        raw = inputs.get("changes") or include.get("changes") or []
        changes = [str(pattern) for pattern in raw]
        if not targets or not changes:
            continue
        for token in targets:
            checked += 1
            if not _changes_names(token, changes):
                uncovered.append(f"{include.get('file')} lints {token}, which no `changes:` names")
    assert checked, f"no include in {label} narrows a lint target — this gate examined nothing"
    assert not uncovered, (
        f"{label}: a lint target outside its own `changes:` list skips the commit "
        "that breaks it:\n  " + "\n  ".join(uncovered)
    )


def test_every_lint_target_is_named_by_its_changes_list(cluster):
    """A narrowed `changes:` must still name every directory the include lints,
    or the one commit that breaks that directory never starts the job."""
    _assert_lint_targets_are_named_by_changes(
        _load_ci(cluster.path / ".gitlab-ci.yml"), cluster.label
    )


def test_this_repositorys_lint_targets_are_named_by_its_changes_list():
    """The same hole in the template's own pipeline, which the render fixtures
    never read."""
    _assert_lint_targets_are_named_by_changes(_load_ci(REPO_ROOT / ".gitlab-ci.yml"), "this repo")


def test_the_target_matcher_rejects_an_unnamed_directory():
    """Mutation proof: a target no glob shares a lineage with must not match."""
    assert _changes_names("scripts", ["scripts/**/*.py"])
    assert _changes_names("lint/", ["lint/yamllint-relaxed.yml"])
    assert _changes_names("template/kubernetes", ["template/**/*.py"])
    assert not _changes_names("docs", ["scripts/**/*.py", ".gitlab-ci.yml"])


def test_every_job_carries_a_runner_tag(cluster):
    """An untagged job lands on whichever runner accepts untagged work: for this
    cluster the shared, non-root, LAN-blocked one, which cannot install packages
    or SSH to a host. Runs on both renders, so a literal tag fails the unlike one.
    """
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    tag = cluster.answers["ci_runner_tag"]
    default_tags = (ci.get("default") or {}).get("tags")
    assert default_tags and tag in default_tags, (
        "the `default:` block does not carry the configured runner tag, so a job "
        f"that names none lands on the untagged runner: {default_tags!r}"
    )
    untagged = []
    for name in sorted(_local_jobs(ci)):
        job = ci[name]
        tags = _resolved(ci, name).get("tags")
        if tags is None:
            # A fragment the LIBRARY defines is not in this file, so it carries
            # no tags here; `default:` above is what tags such a job.
            if any(p in LIB_CI_FRAGMENTS for p in _extends_targets(job)):
                continue
            untagged.append(name)
        else:
            assert tag in tags, f"{name} carries {tags}, not the configured runner tag {tag!r}"
    assert not untagged, "jobs with no runner tag:\n  " + "\n  ".join(untagged)


# --------------------------------------------------------------------------
# Documentation points at things that exist
# --------------------------------------------------------------------------


# A `task <name>` an operator is told to run: backticked in prose, or a command
# line inside a fenced block. Bare prose ("the task is") matches neither, which
# is why the two shapes are matched separately rather than by one \btask\b.
_TASK_NAME = r"([a-z][a-z0-9-]*(?::[a-z0-9-]+)*)(?![a-z0-9:*-])"
_TASK_REF_INLINE = re.compile(r"`task " + _TASK_NAME + r"[^`]*`")
_TASK_REF_FENCE = re.compile(r"^\s*(?:\$\s*)?task " + _TASK_NAME)


def _task_references(text: str):
    """Yield (lineno, task name) for every task reference in one Markdown file."""
    fenced = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        pattern = _TASK_REF_FENCE if fenced else _TASK_REF_INLINE
        for name in pattern.findall(line):
            yield lineno, name


def _task_names(text: str) -> set[str]:
    return {name for _, name in _task_references(text)}


_TEMPLATE_ROOT = Path(__file__).resolve().parent.parent


def _template_docs():
    """This template repo's own docs/ + README. They instruct running tasks in
    the GENERATED repo, so nothing but a render resolves them."""
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
    return set(_taskfile_tree(root))


def test_documented_tasks_exist(cluster, rendered, rendered_b):
    """`task <name>` in operator-facing prose must name a task a generated
    Taskfile defines. The generated repo's docs resolve against THIS render; the
    template's own docs against the union, which covers modules-off renders.
    """
    assert (cluster.path / "Taskfile.yml").is_file(), (
        "the render ships no Taskfile.yml — this gate ran nothing"
    )
    this_render = _defined_tasks(cluster.path)
    generatable = this_render | _defined_tasks(rendered) | _defined_tasks(rendered_b)

    checked = 0
    missing = []
    sources = [(path, text, this_render) for path, text in _text_files(cluster.path)
               if path.suffix == ".md"]
    sources += [(path, text, generatable) for path, text in _template_docs()]
    for path, text, defined in sources:
        for lineno, name in _task_references(text):
            if name.rstrip("*") != name:
                continue  # a prose glob (task terraform:authentik-*)
            checked += 1
            if name not in defined:
                missing.append(f"{path}:{lineno} task {name}")
    assert checked, "no `task <name>` reference was examined — the pattern is stale"
    assert not missing, "documentation names tasks no generated Taskfile defines:\n  " + "\n  ".join(
        missing
    )


# The two prose lists in template/terraform/README.md.jinja that grow an item
# per optional Terraform module, as (regex, what it enumerates) pairs.
_CONJUNCTION_LISTS = (
    (re.compile(r"cluster: (.+?)\. Everything"), "the modules this directory holds"),
    (re.compile(r"for the (.+?) module\.\*\*"), "the modules that refuse -auto-approve"),
    (re.compile(r"a bad apply (.+?), and the plan review"), "what a bad apply costs"),
)


def test_terraform_readme_reads_under_every_optional_module_combination():
    """The prose lists in template/terraform/README.md.jinja must keep their
    conjunction whichever optional modules are on: the "or"/"and" has to sit on
    the last emitted branch. Neither fixture renders a mixed combination.
    """
    src = (_TEMPLATE_ROOT / "template" / "terraform" / "README.md.jinja").read_text(
        encoding="utf-8"
    )
    template = copier_env().from_string(src)
    checked = 0
    for tailscale in (True, False):
        for unifi in (True, False):
            text = " ".join(
                template.render(
                    vpn_tailscale=tailscale, use_unifi=unifi, git_host="git.example.com"
                ).split()
            )
            for pattern, what in _CONJUNCTION_LISTS:
                match = pattern.search(text)
                assert match, f"{what}: pattern {pattern.pattern!r} no longer matches"
                items = [item.strip() for item in match.group(1).split(",")]
                checked += 1
                if len(items) > 1:
                    last = items[-1]
                    joined = last.startswith(("or ", "and ")) or " or " in last or " and " in last
                    assert joined, (
                        f"vpn_tailscale={tailscale} use_unifi={unifi}: {what} renders as a "
                        f"comma list with no conjunction: {match.group(1)!r}"
                    )
    assert checked == 12, "a prose list stopped being examined"


def test_every_rendered_area_is_claimed_by_a_check_or_a_reason(cluster):
    """The validator's check list is hand-maintained against the rendered tree,
    so a new top-level subtree gets no check and nothing notices. Each one is
    either inspected or carries the reason it is not."""
    import validate_render

    areas = sorted(
        path.name
        for path in cluster.path.iterdir()
        if path.is_dir() and path.name not in {".git", "__pycache__"}
    )
    assert areas, "the render ships no directory — this gate examined nothing"
    unclaimed = [name for name in areas if name not in validate_render.RENDERED_AREAS]
    assert not unclaimed, (
        "tests/validate_render.py's RENDERED_AREAS claims no check and no reason for: "
        + ", ".join(unclaimed)
        + " — add the covering check, or the reason none applies"
    )
    inspected = {"ansible", "kubernetes", "terraform", "scripts"}
    assert inspected <= set(areas), (
        "the render is missing the trees the validator inspects: "
        + ", ".join(sorted(inspected - set(areas)))
    )


def test_ci_doc_lists_every_validator_check():
    """docs/CI.md's check table and its `--skip` list must equal main()'s registry.

    The page is where a skip name is looked up when a tool is missing locally.
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


# The answer sets the fixtures above render, by the basename validate-rendered-cluster
# names with --answers.
RENDER_FIXTURE_ANSWERS = {
    render_cluster.ANSWERS.name: "rendered",
    ANSWERS_B.name: "rendered_b",
}


def _render_validate_lines() -> list[tuple[str, list[str]]]:
    """One entry per `validate_render.py` line in the validate-rendered-cluster job:
    (the answers basename it renders, the line's tokens)."""
    job = _load_ci(REPO_ROOT / ".gitlab-ci.yml")["validate-rendered-cluster"]
    lines = []
    for command in job["script"]:
        if "tests/validate_render.py" not in command:
            continue
        tokens = shlex.split(command)
        answers = render_cluster.ANSWERS.name
        for index, token in enumerate(tokens):
            if token == "--answers":
                answers = Path(tokens[index + 1]).name
        lines.append((answers, tokens))
    return lines


def test_every_render_fixture_reaches_the_real_toolchain():
    """validate_render.py is the only place a rendered tree meets yamllint,
    kustomize, kubeconform and the library checkout, and it takes ONE answer set
    per invocation — so a fixture CI stops naming is left to this suite alone."""
    lines = _render_validate_lines()
    assert lines, "validate-rendered-cluster runs no validate_render.py invocation"
    validated = {answers for answers, _ in lines}
    assert validated == set(RENDER_FIXTURE_ANSWERS), (
        "the answer sets validate-rendered-cluster validates and the ones the fixtures render "
        f"differ:\n  CI:       {sorted(validated)}\n  fixtures: "
        f"{sorted(RENDER_FIXTURE_ANSWERS)}"
    )
    unguarded = [answers for answers, tokens in lines if "--lib-path" not in tokens]
    assert not unguarded, (
        "these validate-rendered-cluster lines pass no --lib-path, so every library-reading "
        "check reports a skip instead of a result: " + ", ".join(unguarded)
    )


def test_ci_validates_the_same_mixed_module_set_the_suite_renders():
    """One validate-rendered-cluster line must carry the mixed optional-module overrides,
    and carry MODULES_MIXED exactly, so the toolchain and this suite cannot
    cover different arms."""
    overridden = []
    for answers, tokens in _render_validate_lines():
        data = {
            tokens[index + 1].partition("=")[0]: yaml.safe_load(
                tokens[index + 1].partition("=")[2]
            )
            for index, token in enumerate(tokens)
            if token == "--data"
        }
        if data:
            overridden.append((answers, data))
    assert len(overridden) == 1, (
        "validate-rendered-cluster must pass --data on exactly one validate_render.py line; "
        f"it passes it on {len(overridden)}"
    )
    answers, data = overridden[0]
    assert data == MODULES_MIXED, (
        f"the mixed validate-rendered-cluster line ({answers}) overrides {data}, not the "
        f"MODULES_MIXED set this suite renders: {MODULES_MIXED}"
    )


def test_the_render_independent_checks_run_on_exactly_one_invocation():
    """--self-checks carries the arms that read no render — this repository's own
    pipeline includes and vendored copies. One line must pass it, or they stop
    running; more than one only repeats the output."""
    carriers = [
        (answers, tokens) for answers, tokens in _render_validate_lines()
        if "--self-checks" in tokens
    ]
    assert len(carriers) == 1, (
        "validate-rendered-cluster must pass --self-checks on exactly one validate_render.py "
        f"line; it passes it on {len(carriers)}"
    )
    answers, tokens = carriers[0]
    assert "--lib-path" in tokens, (
        f"the --self-checks line ({answers}) passes no --lib-path, so the vendored "
        "and include-contract checks skip instead of running"
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
# a cluster nobody can sign in to, and per-host log-staleness alerts that never ship.
BRINGUP_MUST_NAME = (
    "certs:show-host-keys",
    "terraform:init",
    "terraform:authentik-apply",
    "flux:sync-host-log-staleness",
)


def test_generated_readme_bringup_matches_setup(cluster):
    """The generated repo's bring-up list and docs/SETUP.md describe one sequence,
    so every task the README names must be named by SETUP, and every step in
    BRINGUP_MUST_NAME must appear in the README.
    """
    readme = cluster.path / "README.md"
    assert readme.is_file(), "the render ships no README.md — this gate ran nothing"
    bringup = _section(readme.read_text(encoding="utf-8"), "Bring-up")
    assert bringup.strip(), "the generated README has no '## Bring-up' section to check"

    setup = (_TEMPLATE_ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")
    setup_tasks = _task_names(setup)
    assert setup_tasks, "no `task <name>` found in docs/SETUP.md — this gate examined nothing"

    readme_tasks = _task_names(bringup)
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
    variable the generated Taskfile does NOT already set. Following one that is
    set produces a duplicate key: go-task takes the last, yamllint fails.
    """
    shipped = set()
    for root in (rendered, rendered_b):
        for text in _taskfile_texts(root):
            shipped |= set(_TF_VAR.findall(text))
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


# Generated file -> the script that generates it. Nothing else compares the two:
# flux-lint substitutes from the ConfigMap and the version bot reads all.yml,
# so both pass on a stale pin.
GENERATED_FILES = {
    "scripts/hosts.env": "generate-hosts-env.py",
    "kubernetes/infrastructure/sources/versions-configmap.yaml": "generate-versions-configmap.py",
    "kubernetes/infrastructure/observability/loki/host-log-staleness.yaml": (
        "generate-host-log-staleness.py"
    ),
}


# go-task's own `{{.VAR}}` reference, and its literal-escape form `{{`...`}}`.
_TASK_VAR = re.compile(r"\{\{\s*\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_TASK_LITERAL = re.compile(r"\{\{`([^`]*)`\}\}")


def _expand_task_vars(text: str, task_vars: dict) -> str:
    """Resolve go-task `{{.VAR}}` references against a Taskfile's `vars:` block.

    To a fixed point, because one var's value may reference another's. Only
    DECLARED names resolve, so kubeconform's `{{.ResourceKind}}` survives.
    """
    def substitute(match: re.Match) -> str:
        value = task_vars.get(match.group(1))
        return match.group(0) if value is None else str(value)

    for _ in range(10):
        expanded = _TASK_VAR.sub(substitute, text)
        if expanded == text:
            break
        text = expanded
    text = _TASK_LITERAL.sub(r"\1", text)
    leftover = sorted({m.group(1) for m in _TASK_VAR.finditer(text)} & set(task_vars))
    assert not leftover, f"unresolved go-task vars in the text under test: {leftover}"
    return text


def _regenerates_and_compares(text: str, target: str, generator: str) -> bool:
    """True when `text` runs the generator and compares the result: either an
    explicit diff against the target, or the generator's own `--check`."""
    if generator not in text:
        return False
    self_checks = re.search(rf"{re.escape(generator)}\s+(?:--[\w-]+\s+)*--check\b", text)
    return bool(self_checks) or ("diff" in text and target in text)


def test_generated_files_are_drift_gated(cluster):
    """A job and a task in the render back the drift gate SETUP.md and
    ARCHITECTURE.md promise for every generated file."""
    root = cluster.path
    ci_text = (root / ".gitlab-ci.yml").read_text(encoding="utf-8")
    ci = _load_ci(root / ".gitlab-ci.yml")
    taskfile = yaml.safe_load((root / "Taskfile.yml").read_text()) or {}
    tasks = _taskfile_tree(root)

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

        # the version-bump bot REGENERATES as part of its own MR, which is the
        # opposite of comparing — it does not count as this gate.
        gating_jobs = [
            name
            for name, job in ci.items()
            if isinstance(job, dict)
            and _regenerates_and_compares(script_text(job), target, generator)
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
    # The task spells paths as go-task vars, so expand them before matching.
    repo_sync = _expand_task_vars(
        script_text(tasks["lint:repo-sync"]), taskfile.get("vars") or {}
    )
    for target, generator in GENERATED_FILES.items():
        assert _regenerates_and_compares(repo_sync, target, generator), (
            f"lint:repo-sync does not regenerate-and-compare {target}"
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
    """A quickstart block is runnable, and an unpinned VCS source already resolves
    to the latest release tag, so a literal `--vcs-ref vX.Y.Z` only goes stale.
    The docs use a `<template-tag>` placeholder.
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


VENDORED_MANIFEST = "scripts/vendored-manifest.yml"
RENDER_VENDORED_GATE = "tests/test_vendored_byte_identity.py"
PYTHON_TESTS_INCLUDE = "/ci/test/python-tests.yml"


def _python_tests_dirs(root: Path) -> list[str]:
    """The directories the render's python-tests job collects, in job order."""
    ci = _load_ci(root / ".gitlab-ci.yml")
    include = next(
        (
            inc for inc in ci["include"]
            if isinstance(inc, dict) and inc.get("file") == PYTHON_TESTS_INCLUDE
        ),
        None,
    )
    assert include, f"the generated pipeline includes no {PYTHON_TESTS_INCLUDE}"
    test_dir = (include.get("inputs") or {}).get("test_dir")
    assert test_dir, (
        f"{PYTHON_TESTS_INCLUDE} is included without a test_dir input, so the job "
        "falls back to the library default"
    )
    return str(test_dir).split()


def test_python_tests_collects_every_suite_directory(cluster):
    """A suite outside `test_dir` never runs in a generated cluster, and
    `task lint:invariants` has to collect the same directories the job does."""
    root = cluster.path
    dirs = _python_tests_dirs(root)
    shipped = sorted({
        path.relative_to(root).parts[0]
        for path in root.rglob("test_*.py")
        if not {"__pycache__", ".git"} & set(path.parts)
    })
    assert shipped, "the render ships no test_*.py at all"
    assert sorted(set(dirs)) == shipped, (
        f"python-tests collects {sorted(set(dirs))} but suites live in {shipped} — "
        "a suite outside test_dir never runs in a generated cluster"
    )
    tasks = _taskfile_tree(root)
    invariants = "\n".join(
        str(step) for step in (tasks.get("lint:invariants") or {}).get("cmds") or []
    )
    assert invariants, "the rendered Taskfile has no lint:invariants cmds"
    assert re.search(r"pytest\s+" + r"\s+".join(re.escape(d) for d in dirs) + r"\b", invariants), (
        f"`task lint:invariants` does not run pytest over {dirs} — the local runner "
        "and the python-tests job would collect different suites"
    )


def _lib_checkout() -> Path | None:
    """A weisssrv-lib checkout this machine can reach, or None.

    Discovered the way the RENDER's own gate discovers one: `$WEISSSRV_LIB_PATH`
    first, then a sibling clone.
    """
    candidates = []
    explicit = os.environ.get("WEISSSRV_LIB_PATH")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(REPO_ROOT.parent / "weisssrv-lib")
    for candidate in candidates:
        if (candidate / "scripts" / "check-vendored-copies.py").is_file():
            return candidate
    return None


# The datreeio CRD-catalog revision kubeconform resolves CR schemas against.
# Held here so the lib_ref bump that moves the library's crd_catalog_ref default
# moves one line, and so CI and `task flux:lint` accept the same schema set.
CRD_CATALOG_REF = "ad3b08c5045129d7bb1eeffd8e61719b2c8dd1e2"


def test_the_crd_catalog_ref_is_pinned_to_the_library_default(cluster):
    """A rendered cluster validates CRs against the same catalog revision its
    pipeline does. An unpinned `main` makes the local gate and CI disagree
    whenever the catalog is rewritten."""
    flux_tasks = (cluster.path / "taskfiles" / "flux.yml").read_text(encoding="utf-8")
    variables = (yaml.safe_load(flux_tasks) or {}).get("vars") or {}
    assert variables.get("CRD_CATALOG_REF") == CRD_CATALOG_REF, (
        f"taskfiles/flux.yml pins CRD_CATALOG_REF {variables.get('CRD_CATALOG_REF')!r}, "
        f"expected {CRD_CATALOG_REF!r}"
    )
    catalog = str(variables.get("CRD_CATALOG", ""))
    assert "{{.CRD_CATALOG_REF}}" in catalog, (
        "CRD_CATALOG spells the revision itself instead of building it from "
        f"CRD_CATALOG_REF: {catalog!r}"
    )
    locations = re.findall(r"-schema-location '([^']+)'", flux_tasks)
    assert locations, "no kubeconform -schema-location in the rendered flux taskfile"
    assert all(location == "{{.CRD_CATALOG}}" for location in locations), (
        f"a kubeconform call site spells its own catalog URL: {sorted(set(locations))}"
    )
    inputs = _flux_lint_include(cluster.path)["inputs"]
    assert str(inputs.get("crd_catalog_ref")) == CRD_CATALOG_REF, (
        f"the flux-lint include passes crd_catalog_ref {inputs.get('crd_catalog_ref')!r}, "
        f"so the pipeline resolves a different catalog than taskfiles/flux.yml's {CRD_CATALOG_REF!r}"
    )
    baseline = inputs.get("expected_skipped_file")
    assert baseline, "flux-lint passes no expected_skipped_file, so a skipped kind only prints"
    assert (cluster.path / baseline).is_file(), f"flux-lint names a missing baseline: {baseline}"


def test_the_crd_catalog_ref_matches_the_librarys_input_default():
    """The pin above is the library flux-lint job's `crd_catalog_ref` default.
    Skipped where no library checkout is reachable."""
    lib = _lib_checkout()
    if lib is None:
        pytest.skip("no weisssrv-lib checkout reachable")
    include = lib / "ci" / "validate" / "flux-lint.yml"
    # The inputs declaration is the FIRST of the file's two documents; load_ci
    # returns the jobs document, which is the last.
    spec = next(
        doc
        for doc in yaml.load_all(include.read_text(), Loader=render_cluster.CILoader)
        if isinstance(doc, dict) and "spec" in doc
    )
    inputs = (spec.get("spec") or {}).get("inputs") or {}
    assert str(inputs["crd_catalog_ref"]["default"]) == CRD_CATALOG_REF, (
        "the library's crd_catalog_ref default moved; bump CRD_CATALOG_REF here and "
        "in template/taskfiles/flux.yml.jinja"
    )


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}


def _duration_seconds(value: str) -> int:
    """Parse a Flux/Helm duration ("15m", "1h30m", "90s") into seconds."""
    parts = re.findall(r"(\d+)([smh])", str(value))
    assert parts, f"unparsable duration {value!r}"
    return sum(int(number) * _DURATION_UNITS[unit] for number, unit in parts)


def _release_timeouts(root: Path, stage: Path):
    """(where, seconds) for every HelmRelease timeout under one stage path."""
    for path in sorted(stage.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "HelmRelease":
                continue
            spec = doc.get("spec") or {}
            for candidate in (
                spec.get("timeout"),
                (spec.get("install") or {}).get("timeout"),
                (spec.get("upgrade") or {}).get("timeout"),
            ):
                if candidate:
                    yield (
                        f"{path.relative_to(root)}:{doc['metadata']['name']}",
                        _duration_seconds(candidate),
                    )


def test_every_waiting_stage_outlasts_its_slowest_release(cluster):
    """A `wait: true` stage must outlast the slowest release it waits on, or the
    stage times out first and the next stage reconciles against a half-installed
    one. A floor only: remediation retries multiply the release timeout.
    """
    root = cluster.path
    clusters = root / "kubernetes" / "clusters" / cluster.answers["cluster_name"]
    assert clusters.is_dir(), f"the render has no {clusters.relative_to(root)}"
    checked, offenders = 0, []
    for path in sorted(clusters.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict) or doc.get("kind") != "Kustomization":
                continue
            spec = doc.get("spec") or {}
            if not (spec.get("wait") and spec.get("timeout") and spec.get("path")):
                continue
            releases = list(_release_timeouts(root, root / spec["path"].lstrip("./")))
            if not releases:
                continue
            checked += 1
            where, slowest = max(releases, key=lambda item: item[1])
            if _duration_seconds(spec["timeout"]) <= slowest:
                offenders.append(
                    f"{doc['metadata']['name']} timeout {spec['timeout']} does not "
                    f"exceed {where} ({slowest}s)"
                )
    assert checked, "no waiting stage was examined — the stage layout moved"
    assert not offenders, "stages that expire before the release they wait on:\n  " + "\n  ".join(
        offenders
    )


RENDERED_RULESET = ".gitlab/secret-detection-ruleset.toml"
LIB_RULESET = "lint/secret-detection-ruleset.toml"


def _ruleset_settings(text: str) -> list[str]:
    """The ruleset's settings, without comments or the per-repo description."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        and not line.lstrip().startswith("description")
    ]


def test_rendered_secret_detection_ruleset_tracks_the_library(cluster):
    """The rendered ruleset is a Jinja source, so it is outside the byte-identity
    gate. Its SHAPE still has to be the library's, or a schema change upstream
    leaves every cluster generated afterwards on the old one."""
    rendered = cluster.path / RENDERED_RULESET
    assert rendered.is_file(), f"the render ships no {RENDERED_RULESET}"
    lib = _lib_checkout()
    if lib is None:
        # A permanent skip in CI is this gate reporting success forever, so the
        # pipeline's job clones the library and a missing checkout is red there.
        if os.environ.get("CI"):
            pytest.fail(
                "CI ships no weisssrv-lib checkout — the ruleset parity gate "
                "would silently skip; set WEISSSRV_LIB_PATH in the python-tests job"
            )
        pytest.skip("no weisssrv-lib checkout to compare against")
    assert _ruleset_settings(rendered.read_text()) == _ruleset_settings(
        (lib / LIB_RULESET).read_text()
    ), (
        f"{RENDERED_RULESET} and the library's {LIB_RULESET} declare different "
        "settings — re-absorb the library's ruleset"
    )


def test_generated_repo_passes_its_own_invariants(cluster):
    tests_dir = cluster.path / "tests"
    # Hard failure, not a skip: this gate's whole value is that a generated
    # cluster runs its own invariants, and a render that stopped shipping tests/
    # would silently switch it off rather than report the regression.
    assert tests_dir.is_dir(), "the render ships no tests/ — this gate ran nothing"
    gate = tests_dir / RENDER_VENDORED_GATE.split("/")[-1]
    assert gate.is_file(), (
        f"the render ships no {RENDER_VENDORED_GATE} — its vendored copies would be "
        "ungated in the generated repository"
    )
    env = dict(os.environ)
    # Do not let this run's cache/rootdir config leak into the nested run.
    env.pop("PYTEST_CURRENT_TEST", None)
    # The directory list comes from the render's own python-tests input, so this
    # gate cannot collect more than a generated cluster's pipeline does.
    dirs = _python_tests_dirs(cluster.path)
    for name in dirs:
        assert (cluster.path / name).is_dir(), (
            f"python-tests names {name}/ but the render ships none — this gate ran nothing"
        )
    argv = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        *[str(cluster.path / name) for name in dirs],
    ]
    # The render's vendored-copy gate never skips: pass the checkout through when
    # one is reachable, deselect the file when it is not. validate_render.py
    # covers byte-identity.
    lib = _lib_checkout()
    if lib:
        env["WEISSSRV_LIB_PATH"] = str(lib)
    else:
        argv.append(f"--ignore={gate}")
    result = subprocess.run(
        argv,
        cwd=cluster.path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, (
        "the generated repository fails its own invariants:\n" + result.stdout + result.stderr
    )


def _manifest_copies(path: Path) -> set[tuple[str, str, str]]:
    """(kind, consumer path, library path) for every entry in a manifest.

    Re-parsed rather than shelled out to the library engine: this suite runs
    with PyYAML alone, and what it asserts is content, not byte-identity.
    """
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path} must contain a mapping"
    assert not set(doc) - {"vendored", "forked"}, (
        f"{path} has sections the library engine does not know: "
        f"{sorted(set(doc) - {'vendored', 'forked'})} — a misspelled one is silently ungated"
    )
    copies = set()
    for kind in ("vendored", "forked"):
        entries = doc.get(kind) or []
        assert isinstance(entries, list), f"{path}: `{kind}:` must be a list"
        for entry in entries:
            if isinstance(entry, str):
                copies.add((kind, entry, entry))
                continue
            assert isinstance(entry, dict) and entry.get("lib"), (
                f"{path}: a {kind} entry needs a `lib:` path — got {entry!r}"
            )
            copies.add((kind, str(entry.get("consumer") or entry["lib"]), str(entry["lib"])))
    return copies


def test_render_manifest_covers_exactly_the_library_copies_it_ships(cluster):
    """The generated cluster's manifest must name every library copy it carries.

    The engine validates what a manifest names and cannot see what it omits, so
    an unregistered copy ships ungated. Byte-identity is validate_render.py's.
    """
    manifest = cluster.path / VENDORED_MANIFEST
    assert manifest.is_file(), (
        f"the render ships no {VENDORED_MANIFEST} — every file it copies from the "
        "library would be ungated in the generated repository"
    )
    rendered = _manifest_copies(manifest)

    # This repository's own manifest is the authority on which files under
    # template/ are library copies, and is itself gated against the library, so
    # it stands in for a library checkout this suite does not have.
    template_copies = {
        (kind, consumer[len("template/") :], lib)
        for kind, consumer, lib in _manifest_copies(REPO_ROOT / VENDORED_MANIFEST)
        if consumer.startswith("template/")
    }
    missing = sorted(consumer for _kind, consumer, _lib in template_copies - rendered)
    assert not missing, (
        "template/scripts/vendored-manifest.yml does not register copies this "
        f"repository's own manifest declares: {missing} — they ship to every generated "
        "cluster ungated"
    )

    # The reverse: a manifest entry is a claim about the render's layout.
    absent = sorted(
        consumer
        for _kind, consumer, _lib in rendered
        if not (cluster.path / consumer).is_file()
    )
    assert not absent, (
        f"the render's {VENDORED_MANIFEST} names files the render does not ship: {absent}"
    )

    # And the other way round for `scripts/`: a manifest that claims a file is a
    # library copy when the template vendors no such thing — site data
    # registered by mistake — holds that file to bytes it was never taken from.
    invented = sorted(
        f"{consumer} (from {lib})"
        for kind, consumer, lib in rendered - template_copies
        if kind == "vendored" and consumer.startswith("scripts/")
    )
    assert not invented, (
        f"the render's {VENDORED_MANIFEST} registers copies this repository does not "
        f"vendor into template/scripts/: {invented}"
    )

    forks = {consumer for kind, consumer, _lib in rendered if kind == "forked"}
    assert forks, (
        f"the render's {VENDORED_MANIFEST} declares no forks — the lint profiles it "
        "ships (ruff.toml, .editorconfig) are forks of the library's, and dropping "
        "them from the manifest is how the library moves under them unnoticed"
    )


def test_registered_copies_are_compared_at_the_pinned_ref(monkeypatch, tmp_path):
    """validate_render's manifest run must pass `--ref WEISSSRV_LIB_REF`.

    Without it the copies are compared against whatever the library checkout
    holds, and the gate tells the operator to re-vendor backwards.
    """
    import validate_render

    lib = tmp_path / "lib"
    (lib / "scripts").mkdir(parents=True)
    (lib / "scripts" / "check-vendored-copies.py").write_text("")
    # The arm reports an empty offer list as having examined nothing, so the stub
    # library has to offer the script the recorded `--list` shape below names.
    (lib / "scripts" / "vendorable-paths.yml").write_text(
        "vendorable:\n  - scripts/check-doc-links.py\n"
    )
    calls: list[list[str]] = []

    def record(cmd, **_kw):
        calls.append(list(cmd))
        # The engine's `--list` shape: the gate reads it for registered copies,
        # and treats an empty listing as a manifest that gates nothing.
        listing = "vendored\tscripts/check-doc-links.py\tscripts/check-doc-links.py\n"
        return subprocess.CompletedProcess(cmd, 0, listing if "--list" in cmd else "", "")

    monkeypatch.setattr(validate_render, "_run", record)

    expected = (
        (_load_ci(REPO_ROOT / ".gitlab-ci.yml").get("variables") or {}).get("WEISSSRV_LIB_REF")
    )
    assert expected, "this repository's .gitlab-ci.yml declares no WEISSSRV_LIB_REF"
    assert not validate_render._check_registered_copies(lib)
    assert calls, "the manifest engine was never invoked"
    assert ["--ref", expected] == calls[0][-2:], (
        f"the engine ran as {calls[0]} — without --ref {expected} it compares this "
        "repository's copies against the library's working tree"
    )

    # No pin to read: the gate must report, never fall back to the working tree.
    calls.clear()
    monkeypatch.setattr(validate_render.render_cluster, "load_ci", lambda _path: {})
    problems = validate_render._check_registered_copies(lib)
    assert not calls, "the engine ran with no resolvable pin"
    assert len(problems) == 1 and "WEISSSRV_LIB_REF" in problems[0], problems


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


# A top-level key in a fenced YAML block: the job name in a worked example.
# `<>` is deliberate — the example names its job `deploy-ansible-<name>`, and a
# placeholder is a key this gate must see rather than skip.
_EXAMPLE_JOB = re.compile(r"^([a-z][a-z0-9<>-]*):$", re.MULTILINE)


def test_ci_doc_example_job_does_not_already_exist(cluster):
    """docs/ci-pipeline.md's "Adding one" block is a job the operator PASTES in.
    If the pipeline already defines that name, following the doc produces a
    duplicate top-level key and the pipeline fails to be created at all."""
    text = (cluster.path / "docs" / "ci-pipeline.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", _section(text, "Deploys") or text, re.DOTALL)
    assert blocks, "no worked YAML example found in docs/ci-pipeline.md — this gate examined nothing"
    example_jobs = {name for block in blocks for name in _EXAMPLE_JOB.findall(block)}
    assert example_jobs, "no job name found in the worked example — the pattern is stale"
    clashing = sorted(example_jobs & set(_load_ci(cluster.path / ".gitlab-ci.yml")))
    assert not clashing, (
        "docs/ci-pipeline.md tells the operator to add jobs .gitlab-ci.yml already "
        "defines: " + ", ".join(clashing)
    )


def test_ci_doc_stages_table_names_every_local_job(cluster):
    """The Stages table is the operator's map of the pipeline, so every local job
    must appear in it. Included library jobs are named there too, but only the
    local ones can be enumerated from the file.
    """
    text = (cluster.path / "docs" / "ci-pipeline.md").read_text(encoding="utf-8")
    stages = _section(text, "Stages")
    assert stages, "docs/ci-pipeline.md has no `## Stages` section — this gate examined nothing"
    documented = set(re.findall(r"`([^`]+)`", stages))
    missing = sorted(_local_jobs(_load_ci(cluster.path / ".gitlab-ci.yml")) - documented)
    assert not missing, (
        "docs/ci-pipeline.md's Stages table does not name these jobs the "
        "pipeline defines: " + ", ".join(missing)
    )


def test_ansible_version_variable_matches_the_deploy_base_input(cluster):
    """`variables.ANSIBLE_VERSION` is what check-deploy-playbooks pip-installs and the
    `ansible_version` include input is what .deploy-base does. Includes resolve
    before job variables exist, so the input repeats the literal.
    """
    ci = _load_ci(cluster.path / ".gitlab-ci.yml")
    declared = (ci.get("variables") or {}).get("ANSIBLE_VERSION")
    assert declared, "the generated pipeline declares no ANSIBLE_VERSION variable"
    inputs = [
        entry["inputs"]["ansible_version"]
        for entry in ci.get("include") or []
        if isinstance(entry, dict) and "ansible_version" in (entry.get("inputs") or {})
    ]
    assert inputs, "no include passes an ansible_version input — the pair this gate holds is gone"
    assert all(str(value) == str(declared) for value in inputs), (
        f"ANSIBLE_VERSION is {declared!r} but an include pins {inputs!r}; check-deploy-playbooks "
        "and .deploy-base would install different Ansibles"
    )


_OP_REF = re.compile(r"op://([^/\s\"']+)/([^/\s\"']+(?: [^/\s\"']+)*)/([^\s\"'`)]+)")

# Both kinds External Secrets Operator offers. A ClusterExternalSecret wraps the
# same spec one level down, under spec.externalSecretSpec.
_ES_KINDS = ("ExternalSecret", "ClusterExternalSecret")


def _remote_keys(doc_yaml: dict) -> list[str]:
    """Every 1Password item title an ExternalSecret-family document names.

    Both shapes count: `data[].remoteRef.key` names one field of an item and
    `dataFrom[].extract.key` pulls the item whole.
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
    """Every secret item the render reads, host-side `op://` references and
    in-cluster remoteRefs alike, is named in PRE-SETUP.md. Otherwise the
    operator learns an item is required by watching a deploy fail."""
    pre_setup = _TEMPLATE_ROOT / "docs" / "PRE-SETUP.md"
    assert pre_setup.is_file(), "docs/PRE-SETUP.md is missing — this gate ran nothing"
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


def _external_secret_data(root: Path):
    """Every `data[]` entry of every ExternalSecret-family document in a render."""
    for path, raw in _k8s_files(root):
        for doc in yaml.safe_load_all(raw):
            if not isinstance(doc, dict) or doc.get("kind") not in _ES_KINDS:
                continue
            spec = doc.get("spec") or {}
            # ClusterExternalSecret nests the ExternalSecret spec it templates out.
            spec = spec.get("externalSecretSpec") or spec
            for entry in spec.get("data") or []:
                if isinstance(entry, dict):
                    yield path, entry


def test_remote_refs_match_the_secrets_backend_convention(cluster):
    """Every remoteRef follows the secrets backend's addressing convention.

    ESO's 1Password Connect provider reads `key` as the item TITLE and
    `property` as the field; a path-shaped key silently syncs an empty Secret.
    """
    backend = cluster.answers["secrets_backend"]
    assert backend == "onepassword", (
        f"secrets_backend {backend!r} renders remoteRefs this gate has no arm for "
        "— add that backend's addressing convention here"
    )

    problems, seen = [], 0
    for path, entry in _external_secret_data(cluster.path):
        seen += 1
        ref = entry.get("remoteRef") or {}
        key = ref.get("key")
        if not ref.get("property"):
            problems.append(f"{path}: remoteRef {key!r} names no property, so no field is read")
        if not isinstance(key, str) or not key.strip():
            problems.append(f"{path}: remoteRef has no key, so no item is addressed")
        elif "/" in key or key.startswith("op:"):
            problems.append(
                f"{path}: remoteRef key {key!r} is a path, not a 1Password item title"
            )
    assert seen, "no ExternalSecret data[] entries in the render — this gate read nothing"
    assert not problems, "\n  ".join(["remoteRefs the Connect provider cannot resolve:", *problems])


# Binaries whose PRE-SETUP entry is the package that provides them.
_TOOL_PACKAGES = {"ansible-playbook": "ansible"}


def test_task_preconditions_name_tools_pre_setup_installs(cluster):
    """Every binary a generated task hard-requires must be in the workstation
    tooling list. A failed go-task precondition aborts the task, so an omission
    is the stranger's first `task lint` stopping on an uninstalled package.
    """
    pre_setup = _TEMPLATE_ROOT / "docs" / "PRE-SETUP.md"
    assert pre_setup.is_file(), "docs/PRE-SETUP.md is missing — this gate ran nothing"
    doc = pre_setup.read_text(encoding="utf-8")
    required = set()
    for text in _taskfile_texts(cluster.path):
        required |= set(re.findall(r"command -v ([\w.+-]+)", text))
    assert required, "no `command -v` precondition found — this gate examined nothing"
    undocumented = sorted(tool for tool in required if _TOOL_PACKAGES.get(tool, tool) not in doc)
    assert not undocumented, (
        "tools a generated task requires but docs/PRE-SETUP.md § 10 never names:\n  "
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
    """A controller HelmRelease with a sibling ExternalSecret sets
    `install.disableWait: true` (ESO installs in the same stage; see
    docs/ARCHITECTURE.md).
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

    A pin with no registry entry is not reported as up to date, it is not
    reported at all. Both fixtures: the registry renders under the same answers.
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


# Every place the generated repo composes a path to its own git repository,
# which is named cluster_name. A mismatch is silent: `flux bootstrap` creates
# the repository it cannot find.
def _repository_paths(root: Path) -> dict[str, str]:
    # vars: are the root file's; the composition itself may sit in a namespace file.
    root_taskfile = (root / "Taskfile.yml").read_text()
    raw = "\n".join(_taskfile_texts(root))
    task_vars = {
        k: str(v) for k, v in ((yaml.safe_load(root_taskfile) or {}).get("vars") or {}).items()
    }

    # The composition only exists once the vars are substituted; comparing the
    # unexpanded literal would compare nothing.
    def expand(value: str) -> str:
        return _expand_task_vars(value, task_vars)

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
    at, `terraform init` 404s on its state, and every runbook link dead-ends.
    """
    expected = f"{cluster.answers['git_namespace']}/{cluster.answers['cluster_name']}"
    paths = _repository_paths(cluster.path)
    # Named explicitly: a regex that stopped matching would silently reduce
    # this assertion to a no-op.
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
        # Anchor on the filename, not a /docs/ path segment: the annotation
        # value keeps the path inside an unexpanded ConfigMap reference.
        for m in re.finditer(r"runbook_url:.*?([A-Za-z0-9_.-]+\.md)(#[\w-]+)?", raw):
            checked += 1
            target = rendered / "docs" / m.group(1)
            if not target.is_file():
                broken.append(f"{path.relative_to(rendered)} -> docs/{m.group(1)} (missing)")
            elif m.group(2) and m.group(2).lstrip("#") not in _anchors(target):
                broken.append(f"{path.relative_to(rendered)} -> docs/{m.group(1)}{m.group(2)} (no such heading)")
    # A gate that examines nothing passes forever.
    assert checked, "no runbook_url annotations were examined — the pattern is stale"
    assert not broken, "alert runbook_url annotations do not resolve:\n  " + "\n  ".join(sorted(set(broken)))


def test_runbook_index_lists_every_section():
    """docs/RUNBOOKS.md is the index of template/docs/RUNBOOKS.md.jinja and says
    so; nothing but this holds the two in sync."""
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


# CI runner sizing. partials/ci-sizing.jinja holds both runner tiers' capacity
# model; these gates hold it to the reference cluster it was calibrated against
# and to the invariants a quota must satisfy.

# The reference cluster's live values, at the roster it runs (5 compute hosts).
# A derivation that no longer reproduces them has been re-tuned by accident.
REFERENCE_COMPUTE_NODES = 5
REFERENCE_SIZING = {
    "privileged": {"concurrent": 12, "cpu": 38, "mem": 25, "limit_mem": 82, "pods": 25},
    "shared": {"concurrent": 7, "cpu": 8, "mem": 16, "limit_mem": 46, "pods": 20},
}


def _sizing(compute_node_count: int):
    """Render partials/ci-sizing.jinja and return its exported names."""
    env = copier_env(loader=jinja2.FileSystemLoader(str(REPO_ROOT)))
    module = env.get_template("partials/ci-sizing.jinja").make_module(
        {"compute_node_count": compute_node_count}
    )
    return {name: getattr(module, name) for name in dir(module) if not name.startswith("_")}


def _quota(model: dict, tier: str) -> dict:
    return {
        "concurrent": model[f"{tier}_concurrent"],
        "cpu": model[f"{tier}_quota_cpu"],
        "mem": model[f"{tier}_quota_mem_gib"],
        "limit_mem": model[f"{tier}_quota_limit_mem_gib"],
        "pods": model[f"{tier}_quota_pods"],
    }


@pytest.mark.parametrize("tier", ["privileged", "shared"])
def test_ci_sizing_reproduces_the_reference_cluster(tier):
    """Both tiers are calibrated: at the reference roster the derivation must
    emit exactly what that cluster runs, or the model has drifted off the only
    values anyone has operated."""
    assert _quota(_sizing(REFERENCE_COMPUTE_NODES), tier) == REFERENCE_SIZING[tier]


@pytest.mark.parametrize("compute", [2, REFERENCE_COMPUTE_NODES])
def test_shared_quota_stays_inside_the_agent_pool(compute):
    """The shared tier co-tenants every agent, so its reservations stay a minority
    of the pool. The privileged tier is deliberately not held to this: its
    overflow queues on the `ci-jobs` PriorityClass.
    """
    model = _sizing(compute)
    quota = _quota(model, "shared")
    assert quota["cpu"] <= 2 / 3 * model["shared_pool_cores"]
    assert quota["mem"] <= 2 / 3 * model["shared_pool_mem_gib"]
    burst = quota["concurrent"] * model["shared_job_mem_limit_gib"]
    assert burst <= model["shared_pool_fraction"] * model["shared_pool_mem_gib"]
    if compute == 2:
        # The default roster is the smallest one shipped, and the tightest.
        assert burst <= 2 / 3 * model["shared_pool_mem_gib"]


@pytest.mark.parametrize("compute", [2, REFERENCE_COMPUTE_NODES])
def test_privileged_concurrent_fits_the_schedulable_pool(compute):
    """The privileged tier is CPU-bound and sits at the edge of its pool: the
    bound is the pool minus the platform holdback, and it must be the largest
    value that fits or the tier is undersized.
    """
    model = _sizing(compute)
    scheduled = model["privileged_concurrent"] * model["privileged_job_cpu"]
    headroom = model["privileged_pool_cores"] - 2
    assert scheduled <= headroom
    assert scheduled > headroom - model["privileged_job_cpu"]
    footprint = model["privileged_concurrent"] * model["privileged_job_mem_gib"]
    assert footprint <= 2 / 3 * model["privileged_pool_mem_gib"]


@pytest.mark.parametrize("tier", ["privileged", "shared"])
@pytest.mark.parametrize("compute", [1, 2, 4, REFERENCE_COMPUTE_NODES, 8])
def test_quota_admits_every_job_the_runner_submits(tier, compute):
    """Every quota dimension admits `concurrent` jobs with the manager pod
    resident. A quota below that does not throttle: the 403 reaches GitLab as
    a job failure with no retry."""
    model = _sizing(compute)
    quota = _quota(model, tier)
    admitted = min(
        (quota["cpu"] - model["manager_cpu"]) // model[f"{tier}_job_cpu"],
        (quota["mem"] - model["manager_mem_gib"]) // model[f"{tier}_job_mem_gib"],
        (quota["limit_mem"] - model["manager_mem_limit_gib"])
        // model[f"{tier}_job_mem_limit_gib"],
        quota["pods"] - 1,
    )
    assert admitted >= quota["concurrent"], (
        f"{tier} at compute={compute}: quota admits {admitted} jobs but concurrent is "
        f"{quota['concurrent']}"
    )


def test_sizing_constants_match_the_rendered_inventory(cluster):
    """The capacity model derives every runner number from the agent VM size, so
    the inventory that builds those VMs has to agree. Shrinking an agent
    otherwise ships a quota calibrated to a pool that no longer exists."""
    model = _sizing(cluster.answers["compute_node_count"])
    hosts = yaml.safe_load(
        (cluster.path / "ansible" / "inventories" / "prod" / "hosts.yml").read_text()
    )
    agents = hosts["all"]["children"]["k3s_agents"]["hosts"]
    assert agents, "the render has no k3s_agents hosts"
    mismatched = [
        f"{name}: {(vars_ or {}).get('proxmox_vm_cores')} cores / "
        f"{(vars_ or {}).get('proxmox_vm_memory')}Mi"
        for name, vars_ in agents.items()
        if (vars_ or {}).get("proxmox_vm_cores") != model["agent_cores"]
        or (vars_ or {}).get("proxmox_vm_memory") != model["agent_mem_gib"] * 1024
    ]
    assert not mismatched, (
        f"agents sized unlike partials/ci-sizing.jinja ({model['agent_cores']} cores / "
        f"{model['agent_mem_gib'] * 1024}Mi):\n  " + "\n  ".join(mismatched)
    )


@pytest.mark.parametrize("tier", ["privileged", "shared"])
def test_rendered_runner_numbers_come_from_the_sizing_model(cluster, tier):
    """Wiring: the shipped manifests must BE the model's output. `concurrent`
    and the quota live in two files, so a hand-edit to either is the drift this
    single-sourcing exists to prevent."""
    app = "gitlab-runner-privileged" if tier == "privileged" else "gitlab-runner"
    app_dir = cluster.path / "kubernetes" / "apps" / app
    assert app_dir.is_dir(), (
        f"{app} is missing from the render (git_backend={cluster.answers['git_backend']})"
    )
    expected = _quota(_sizing(cluster.answers["compute_node_count"]), tier)
    release = yaml.safe_load((app_dir / "release.yaml").read_text())
    quota = next(
        doc
        for doc in yaml.safe_load_all((app_dir / "resourcequota.yaml").read_text())
        if doc and doc["kind"] == "ResourceQuota"
    )["spec"]["hard"]
    assert release["spec"]["values"]["concurrent"] == expected["concurrent"]
    assert quota["requests.cpu"] == str(expected["cpu"])
    assert quota["requests.memory"] == f"{expected['mem']}Gi"
    assert quota["limits.memory"] == f"{expected['limit_mem']}Gi"
    assert quota["pods"] == str(expected["pods"])


# Integration-test scaffolding: the multi-role stacks a generated cluster ships,
# the shared molecule prep they import, and the CI matrix that runs them.

SHARED_MOLECULE_FILES = (
    "prepare-common.yml",
    "tasks/container-warmup.yml",
    "tasks/prepare-apt-disable.yml",
    "tasks/prepare-base.yml",
)


def _scenarios(cluster: Cluster) -> dict[str, Path]:
    """Scenario name -> its molecule/default directory."""
    root = cluster.path / "ansible" / "integration-tests"
    return {
        d.name: d / "molecule" / "default"
        for d in sorted(root.iterdir())
        if d.is_dir() and not d.name.startswith("_")
    }


def test_every_stack_scenario_renders_complete(cluster):
    """A scenario missing converge or verify fails only when molecule runs it,
    which is a privileged-runner job away from the change that broke it."""
    expected = {"base-infrastructure", "cert-distribution", "dns-stack", "mail-stack"}
    if cluster.answers["storage_backend"] == "zfs":
        expected.add("storage-stack")
    scenarios = _scenarios(cluster)
    assert set(scenarios) == expected
    for name, default in scenarios.items():
        for filename in ("molecule.yml", "converge.yml", "verify.yml"):
            path = default / filename
            assert path.is_file(), f"{name} renders no {filename}"
            assert list(yaml.safe_load_all(path.read_text())), f"{name}/{filename} is empty"


def test_scenarios_reach_the_shared_prep_and_the_pinned_inventory(cluster):
    """Every converge loads the production all.yml and the shared warm-up, and
    every scenario resolves the collection pin from ansible/requirements.yml —
    relative paths that a directory move silently breaks."""
    molecule_dir = cluster.path / "ansible" / "molecule"
    for name in SHARED_MOLECULE_FILES:
        assert (molecule_dir / name).is_file(), f"ansible/molecule/{name} did not render"
    for name, default in _scenarios(cluster).items():
        converge = (default / "converge.yml").read_text()
        assert "../../../../inventories/prod/group_vars/all.yml" in converge, name
        assert "../../../../molecule/tasks/container-warmup.yml" in converge, name
        for path in (
            default / "../../../../inventories/prod/group_vars/all.yml",
            default / "../../../../molecule/tasks/container-warmup.yml",
        ):
            assert path.resolve().is_file(), f"{name}: {path} does not resolve"
        molecule = yaml.safe_load((default / "molecule.yml").read_text())
        options = ((molecule.get("dependency") or {}).get("options") or {})
        assert options.get("requirements-file") == "../../requirements.yml", name
        assert (default / "../../../../requirements.yml").resolve().is_file(), name


def test_scenario_images_track_the_pinned_library_ref(cluster):
    """The local fallback tag and the CI override must name the same build: a
    scenario left on an older tag tests roles the cluster does not install."""
    ref = yaml.safe_load((REPO_ROOT / "copier.yml").read_text())["lib_ref"]["default"]
    for name, default in _scenarios(cluster).items():
        for platform in yaml.safe_load((default / "molecule.yml").read_text())["platforms"]:
            image = platform["image"]
            assert "MOLECULE_TEST_IMAGE" in image, f"{name} pins no CI override"
            assert image.rstrip("}").endswith(f"molecule-test:{ref}"), (
                f"{name} falls back to {image}, not the pinned {ref}"
            )


def test_the_ci_matrix_runs_exactly_the_rendered_scenarios(cluster):
    """`scripts/check-integration-matrix-coverage.py` is the shipped gate; this
    is the render-time half, so a conditional scenario cannot ship unrun."""
    jobs = _load_ci(cluster.path / ".gitlab" / "ci" / "integration-jobs.yml")
    matrix = jobs["integration-tests"]["parallel"]["matrix"]
    in_ci = {name for entry in matrix for name in entry["TEST"]}
    assert in_ci == set(_scenarios(cluster))
    check = subprocess.run(
        [sys.executable, "scripts/check-integration-matrix-coverage.py"],
        cwd=cluster.path,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_the_junit_sanitizer_runs_strict(cluster):
    """Without --strict a declaration that matches nothing only warns, so a
    renamed guard leaves the integration job green on a test it no longer runs."""
    script = _load_ci(cluster.path / ".gitlab" / "ci" / "integration-jobs.yml")
    commands = (script[".integration-test-job"] or {}).get("script") or []
    calls = [c for c in commands if "sanitize-junit-expected-failures.py" in str(c)]
    assert len(calls) == 1, "expected one sanitize-junit-expected-failures.py call"
    assert "--strict" in str(calls[0]), (
        "the sanitize call does not pass --strict, so a stale expectation only warns"
    )


def test_the_taskfile_runs_exactly_the_rendered_scenarios(cluster):
    """`task ansible:test-integration` is the third copy of the scenario list and
    the only one no shipped gate holds: a scenario it omits is skipped locally
    while the CI matrix runs it."""
    tasks = _taskfile_tree(cluster.path)
    per_scenario = {}
    for name, task in tasks.items():
        for cmd in (task or {}).get("cmds") or []:
            if not isinstance(cmd, dict):
                continue
            molecule_dir = (cmd.get("vars") or {}).get("MOLECULE_DIR", "")
            if "integration-tests/" in molecule_dir:
                per_scenario[name] = molecule_dir.rstrip("/").rsplit("/", 1)[-1]
    assert per_scenario, "the rendered Taskfile runs no molecule scenario — this gate is examining nothing"
    assert set(per_scenario.values()) == set(_scenarios(cluster))
    aggregator = tasks.get("ansible:test-integration") or {}
    # A `task:` reference inside a namespace file names a sibling, so qualify the
    # bare form before comparing it with the fully qualified names.
    aggregated = set()
    for cmd in aggregator.get("cmds") or []:
        if isinstance(cmd, dict) and "task" in cmd:
            ref = str(cmd["task"]).lstrip(":")
            aggregated.add(ref if ":" in ref else f"ansible:{ref}")
    missing = sorted(set(per_scenario) - aggregated)
    assert not missing, (
        "`task ansible:test-integration` never reaches these scenario tasks:\n  "
        + "\n  ".join(missing)
    )


# Pins both pipelines still spell out. The binary pins moved into the vendored
# scripts/ci-fetch-tools.py, byte-identical in both trees.
SHARED_TOOL_PINS = ("PYYAML_VERSION",)
FETCHER_RELPATH = "scripts/ci-fetch-tools.py"
# Release-asset markers for the tools the fetcher owns. A pipeline naming one
# is downloading it by hand against its own pin.
FETCHED_TOOL_MARKERS = (
    "releases/download/kustomize",
    "yannh/kubeconform/releases",
    "prometheus/prometheus/releases",
    "prometheus/alertmanager/releases",
    "releases.hashicorp.com/terraform",
    "koalaman/shellcheck/releases",
    "jqlang/jq/releases",
)


def test_both_pipelines_fetch_binaries_from_the_vendored_fetcher(cluster):
    """One pin set for both. A pipeline downloading a tool by hand can pin a
    version this repository's own gate never ran."""
    for root in (REPO_ROOT, cluster.path):
        assert (root / FETCHER_RELPATH).is_file(), f"{root} ships no {FETCHER_RELPATH}"
        ci = (root / ".gitlab-ci.yml").read_text(encoding="utf-8")
        assert FETCHER_RELPATH in ci, f"{root}/.gitlab-ci.yml never calls {FETCHER_RELPATH}"
        hand_rolled = sorted(n for n in FETCHED_TOOL_MARKERS if n in ci)
        assert not hand_rolled, (
            f"{root}/.gitlab-ci.yml still downloads {hand_rolled} by hand — those "
            f"pins belong in {FETCHER_RELPATH}"
        )


def test_the_two_pipelines_pin_the_same_tools(cluster):
    """Drift between the two sets of pins means this repository's gate and every
    generated cluster disagree about what a valid alert rule or manifest is."""
    root = _load_ci(REPO_ROOT / ".gitlab-ci.yml")["variables"]
    generated = _load_ci(cluster.path / ".gitlab-ci.yml")["variables"]
    missing = sorted(n for n in SHARED_TOOL_PINS if n not in root or n not in generated)
    assert not missing, (
        f"tool pins absent from one of the two pipelines: {missing} — rename both "
        "sides together, or drop the name from SHARED_TOOL_PINS"
    )
    mismatch = {
        name: (root[name], generated[name])
        for name in SHARED_TOOL_PINS
        if root[name] != generated[name]
    }
    assert not mismatch, f"the two pipelines pin different tool versions: {mismatch}"


def _assert_only_role(root: Path, when: str) -> tuple[Path, Path]:
    """A minimal library and render carrying one role whose only assert sits
    behind `when`, which is all check_required_role_inputs reads."""
    lib = root / "lib"
    role = lib / "ansible_collections/weisssrv/infra/roles/demo"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "defaults/main.yml").write_text("demo_target: ''\n")
    (role / "tasks/main.yml").write_text(
        "- name: Require the target\n"
        "  ansible.builtin.assert:\n"
        "    that:\n"
        "      - demo_target | default('') | length > 0\n"
        f"  when: {when}\n"
    )
    render = root / "render"
    (render / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "template" / "scripts" / "check-role-inputs.py", render / "scripts")
    (render / "ansible/inventories/prod/group_vars").mkdir(parents=True)
    (render / "ansible/inventories/prod/group_vars/all.yml").write_text("demo_target: demo-host\n")
    (render / "ansible/playbooks").mkdir(parents=True)
    (render / "ansible/playbooks/site.yml").write_text(
        "- name: Demo\n  hosts: all\n  roles:\n    - role: weisssrv.infra.demo\n"
    )
    return render, lib


def test_required_role_inputs_reads_a_modelled_assert(tmp_path):
    """The control for the negative case below: an evaluable `when:` keeps the
    role's asserted input in the required set and the check passes."""
    import validate_render

    render, lib = _assert_only_role(tmp_path, "true")
    validate_render.check_required_role_inputs(render, lib_path=lib)


def test_required_role_inputs_names_an_expression_it_cannot_evaluate(tmp_path):
    """An unevaluable `when:` used to drop the role's asserted inputs from the
    required set in silence, leaving a generated cluster to die on that assert."""
    import validate_render

    render, lib = _assert_only_role(tmp_path, "demo_target | basename != ''")
    with pytest.raises(validate_render.Failure) as raised:
        validate_render.check_required_role_inputs(render, lib_path=lib)
    message = str(raised.value)
    assert "demo/tasks/main.yml" in message, message
    assert "basename" in message, message


# --------------------------------------------------------------------------
# The inventory's addressing scheme
# --------------------------------------------------------------------------


def _inventory_hosts(group):
    """Yield every (name, vars) host pair under an inventory group, recursively."""
    if not isinstance(group, dict):
        return
    for name, host_vars in (group.get("hosts") or {}).items():
        yield name, host_vars or {}
    for child in (group.get("children") or {}).values():
        yield from _inventory_hosts(child)


def test_guest_vmid_is_derived_from_its_address(cluster):
    """hosts.yml's header states `vmid = 100 + last octet`, and the resolvers
    compute theirs that way while the k3s guests number themselves off a loop
    index. A re-addressed guest keeping its old vmid collides with another."""
    inventory = yaml.safe_load(
        (cluster.path / "ansible" / "inventories" / "prod" / "hosts.yml").read_text()
    )
    seen = 0
    offenders = []
    for name, host_vars in _inventory_hosts(inventory.get("all")):
        if "vmid" not in host_vars or "ansible_host" not in host_vars:
            continue
        seen += 1
        expected = 100 + int(str(host_vars["ansible_host"]).rsplit(".", 1)[1])
        if int(host_vars["vmid"]) != expected:
            offenders.append(
                f"{name}: {host_vars['ansible_host']} carries vmid {host_vars['vmid']}, "
                f"expected {expected}"
            )
    assert seen, "no guest declares both a vmid and an address — this gate checked nothing"
    assert not offenders, (
        "vmid must be 100 + the address's last octet, as hosts.yml states:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Composed addresses vs. the reserved bands
# --------------------------------------------------------------------------


def _reserved_address_bands() -> list[tuple[int, int, str]]:
    """copier.yml's reserved_address_bands, read from the config itself so this
    gate cannot drift from the list every address validator iterates."""
    config = yaml.safe_load((REPO_ROOT / "copier.yml").read_text())
    bands = yaml.safe_load(config["reserved_address_bands"]["default"])
    return [(int(low), int(high), str(what)) for low, high, what in bands]


def _inventory_addresses(group: dict):
    """Every (host, ansible_host) an inventory group holds, children included."""
    for name, host_vars in (group.get("hosts") or {}).items():
        address = (host_vars or {}).get("ansible_host")
        if address:
            yield name, str(address)
    for child in (group.get("children") or {}).values():
        if child:
            yield from _inventory_addresses(child)


def test_composed_addresses_stay_inside_the_reserved_bands(cluster):
    """partials/roster.jinja composes the guest addresses the band validators
    reserve for them. An offset moved there without moving the band lets an
    operator be prompted onto an address a generated guest already holds."""
    bands = _reserved_address_bands()
    prefix = cluster.answers["lan_prefix"]
    resolvers = set(cluster.answers["upstream_dns_servers"].split())
    hosts = yaml.safe_load(
        (cluster.path / "ansible" / "inventories" / "prod" / "hosts.yml").read_text()
    )
    # Resolver addresses ARE the upstream_dns_servers answer, so they sit
    # outside the bands by design.
    composed = {
        (name, int(address.rsplit(".", 1)[1]))
        for name, address in _inventory_addresses(hosts["all"])
        if address.startswith(prefix + ".") and address not in resolvers
    }
    assert composed, "no composed address was collected — this gate checked nothing"
    listed = "\n  ".join(f".{low}-.{high}: {what}" for low, high, what in bands)
    outside = [
        f"{name} at {prefix}.{octet}"
        for name, octet in sorted(composed)
        if not any(low <= octet <= high for low, high, _ in bands)
    ]
    assert not outside, (
        "composed addresses outside copier.yml reserved_address_bands:\n  "
        + "\n  ".join(outside)
        + f"\nbands:\n  {listed}"
    )
    unclaimed = [
        f".{low}-.{high}: {what}"
        for low, high, what in bands
        if not any(low <= octet <= high for _, octet in composed)
    ]
    assert not unclaimed, (
        "reserved bands no composed address uses, so the LAN is over-reserved:\n  "
        + "\n  ".join(unclaimed)
    )


# --------------------------------------------------------------------------
# Rendered workload resources
# --------------------------------------------------------------------------

# Multipliers for the quantity suffixes the shipped manifests use. A suffix not
# listed here is skipped rather than guessed at.
_QUANTITY_SUFFIXES = {
    "": 1,
    "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
}

_QUANTITY = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<suffix>[A-Za-z]*)$")

# cpu is left out on purpose: a burstable workload requests less cpu than it may
# use, and most shipped releases set no cpu limit at all.
COMPARED_RESOURCES = ("memory", "ephemeral-storage")


def _quantity(value) -> float | None:
    """A Kubernetes quantity as bytes, or None when it is not comparable."""
    match = _QUANTITY.match(str(value).strip())
    if not match or match["suffix"] not in _QUANTITY_SUFFIXES:
        return None
    return float(match["number"]) * _QUANTITY_SUFFIXES[match["suffix"]]


def _manifest_files(root: Path) -> list[Path]:
    """Every YAML manifest under `root`, both suffixes."""
    return sorted(path for suffix in ("*.yml", "*.yaml") for path in root.rglob(suffix))


def _resource_blocks(node):
    """Every mapping in a parsed document that sets both requests and limits.

    A recursive walk, not a per-app path list: a container spec, a HelmRelease
    `values:` tree and a CRD template spell it alike, so a new app is covered.
    """
    if isinstance(node, dict):
        if isinstance(node.get("requests"), dict) and isinstance(node.get("limits"), dict):
            yield node
        for value in node.values():
            yield from _resource_blocks(value)
    elif isinstance(node, list):
        for value in node:
            yield from _resource_blocks(value)


def inverted_resources(root: Path) -> list[str]:
    """Rendered resource blocks that request more than they are allowed."""
    inverted = []
    for path in _manifest_files(root / "kubernetes"):
        for doc in yaml.safe_load_all(path.read_text()):
            for block in _resource_blocks(doc):
                for name in COMPARED_RESOURCES:
                    want = _quantity(block["requests"].get(name))
                    cap = _quantity(block["limits"].get(name))
                    if want is not None and cap is not None and want > cap:
                        inverted.append(
                            f"{path.relative_to(root)}: {name} requests "
                            f"{block['requests'][name]} > limits {block['limits'][name]}"
                        )
    return inverted


def test_no_rendered_workload_requests_more_than_its_limit(cluster):
    """The kubelet rejects such a pod outright, and neither kustomize build nor
    kubeconform sees it: the inversion surfaces at reconcile time."""
    blocks = sum(
        1
        for path in _manifest_files(cluster.path / "kubernetes")
        for doc in yaml.safe_load_all(path.read_text())
        for _block in _resource_blocks(doc)
    )
    assert blocks > 20, f"the walk found only {blocks} resource blocks — it matched nothing"
    inverted = inverted_resources(cluster.path)
    assert not inverted, "rendered resource requests exceed their limits:\n  " + "\n  ".join(
        inverted
    )


def test_an_inverted_resource_block_is_reported(tmp_path):
    """Mutation case: the shape a retune takes when only one half is edited."""
    manifest = tmp_path / "kubernetes" / "apps" / "x" / "deployment.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "spec:\n  template:\n    spec:\n      containers:\n        - name: x\n"
        "          resources:\n"
        "            requests:\n              memory: 1Gi\n              cpu: 2\n"
        "            limits:\n              memory: 512Mi\n"
    )
    found = inverted_resources(tmp_path)
    assert len(found) == 1 and "memory requests 1Gi > limits 512Mi" in found[0], found


def test_a_quantity_with_an_unknown_suffix_is_skipped():
    """A `${...}` placeholder or an unrecognised suffix must not read as zero."""
    assert _quantity("${cluster_memory}") is None
    assert _quantity("128Mi") == 128 * 2**20
    assert _quantity("1G") == 10**9
