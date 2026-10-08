"""Every alert rule must have a promtool unit test, or a declared exemption.

The corpus is the extracted rules, every PrometheusRule CR and the Loki rule
files. The UNTESTED_* sets are hand-kept, one claim per group.
"""

from __future__ import annotations

import re

import pytest
import yaml
from conftest import REPO, alert_rules, assert_all_parsed

TESTS_DIR = REPO / "tests" / "prometheus-rules"

# Alerts with no promtool unit test, grouped by why. Each group is a testable
# claim about the expression, checked by the shape gate below. The lists only
# shrink: an entry naming a dropped alert, or one that now has a test, fails.

# Plain comparisons: no range window, no `absent()`, no set operator and no
# function call, so a unit test would assert what the expression already reads as.
UNTESTED_BARE = {
    "ArchiveBackupDatasetStale",
    "ArchiveBackupFailed",
    "BlackboxCertExpiringSoon",
    "CertExpiringCritical",
    "CertExpiringWarning",
    "CertificateNotReady",
    "DiskUsageCritical",
    "DiskUsageWarning",
    "ExternalSecretSyncFailure",
    "FluxResourceNotReady",
    "InodeUsageCritical",
    "InodeUsageWarning",
    "KuredRebootStuck",
    "LocalPathPVExists",
    "MaintenanceRebootDeferred",
    "NodeMemoryPressure",
    "NodeStuckCordoned",
    "OffsiteBackupFailed",
    "PVCUsageCritical",
    "PVCUsageWarning",
    "PveClusterBackupFailed",
    "VzdumpBackupFailed",
    "ZFSPoolDataErrors",
    "ZFSPoolNotOnline",
}

# Windowed, joined, aggregated or absent() shapes. `promtool check rules` only
# proves these parse: an aggregation that collapses every series, or a window
# shorter than the scrape gap, reads as a pass. Each of these is owed a test.
UNTESTED_SHAPES_OWED = {
    "BlackboxExporterDown",
    "EndpointDown",
    "EtcdSnapshotStale",
    "FluxReconciliationFailure",
    "LokiRulerEvaluationFailures",
    "LokiRulerNotificationFailures",
    "LokiRulerRulesMissing",
    "OffsiteBackupVerifyStale",
    "RegistryCacheDown",
    "SecretsProviderDown",
    "ZFSPoolDeviceErrors",
}

# Gated on a `${cluster_*}` placeholder. The extractor does not substitute, so
# that absent() arm matches nothing and fires at every eval_time: a test here
# would assert the harness, not the alert.
UNTESTED_PLACEHOLDER_GATED = {
    "ArchiveBackupStale",
    "BackupArtifactStale",
    "KeyEndpointProbeMissing",
    "OffsiteBackupStale",
    "VzdumpBackupStale",
}

# LogQL rule files the Loki ruler loads, not PrometheusRule CRs. promtool can
# neither parse nor evaluate them, so coverage needs a logcli suite.
UNTESTED_LOGQL = {
    "HostLogShippingStaleAll",
    "GitlabRunnerReaperPartialSweep",
}

UNTESTED = (
    UNTESTED_BARE
    | UNTESTED_SHAPES_OWED
    | UNTESTED_PLACEHOLDER_GATED
    | UNTESTED_LOGQL
)


def uncovered(corpus: set[str], tested: set[str]) -> list[str]:
    """Alertnames with neither a unit test nor an UNTESTED entry."""
    return sorted(corpus - tested - UNTESTED)


@pytest.fixture(scope="module")
def rules() -> dict[str, dict]:
    """alertname -> rule, for every alert the repository ships."""
    found, unreadable = alert_rules()
    # A file that will not parse shrinks the corpus, and a shrunk corpus reads as
    # full coverage.
    assert_all_parsed(unreadable, "alert")
    assert found, "the corpus holds no alerts — the gate would be vacuous"
    by_name: dict[str, dict] = {}
    for alert, rule in found:
        by_name.setdefault(alert, rule)
    return by_name


@pytest.fixture(scope="module")
def corpus(rules) -> set[str]:
    """Every alertname the repository ships: release values, CRs and Loki rules."""
    return set(rules)


@pytest.fixture(scope="module")
def tested() -> set[str]:
    """Every alertname a *.test.yaml asserts FIRING at least once."""
    names: set[str] = set()
    for path in sorted(TESTS_DIR.glob("*.test.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for case in doc.get("tests") or []:
            for assertion in case.get("alert_rule_test") or []:
                if assertion.get("alertname") and assertion.get("exp_alerts"):
                    names.add(assertion["alertname"])
    return names


def test_every_alert_is_tested_or_declared_untested(corpus, tested):
    """tests/prometheus-rules/README.md holds the conventions a case must follow
    to test anything: the in-hold arm, the series range, the healthy margin."""
    missing = uncovered(corpus, tested)
    assert not missing, (
        "alerts with no promtool unit test and no UNTESTED entry:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd a case to tests/prometheus-rules/<area>.test.yaml, or add the "
        "name to UNTESTED in this file under the group that says why."
    )


def test_untested_entries_still_name_a_live_alert(corpus):
    """A stale exemption silently covers nothing and hides the next omission."""
    stale = sorted(UNTESTED - corpus)
    assert not stale, (
        f"UNTESTED names alerts the corpus no longer defines: {stale} — drop them"
    )


def test_untested_does_not_cover_an_alert_that_now_has_a_test(corpus, tested):
    """The exemption list must shrink as tests land, not linger."""
    redundant = sorted(UNTESTED & tested)
    assert not redundant, (
        f"these are exempt AND tested: {redundant} — remove them from UNTESTED"
    )


_RANGE_WINDOW = re.compile(r"\[\s*\d+(?:\.\d+)?[smhdwy]")
_SET_OP = re.compile(r"\b(?:and|or|unless|group_left|group_right)\b|\b(?:on|ignoring)\s*\(")
_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\s*(?:(?:by|without)\s*\([^)]*\)\s*)?\(")
# time() takes no vector, so it cannot collapse or drop a series.
_SCALAR_FUNCTIONS = {"time"}


def bare_shape_violations(exprs: dict[str, str]) -> list[str]:
    """Alerts filed as plain comparisons whose expression is not one."""
    violations = []
    for alert, expr in sorted(exprs.items()):
        text = " ".join(str(expr).split())
        reasons = []
        if _RANGE_WINDOW.search(text):
            reasons.append("a range window")
        if _SET_OP.search(text):
            reasons.append("a set operator")
        calls = sorted({name for name in _CALL.findall(text) if name not in _SCALAR_FUNCTIONS})
        if calls:
            reasons.append(f"a function call ({', '.join(calls)})")
        if reasons:
            violations.append(f"{alert}: {', '.join(reasons)}")
    return violations


def test_every_bare_exemption_really_is_a_plain_comparison(rules):
    """An entry filed under the wrong group is an exemption nobody re-reads: the
    group's claim is why no unit test is owed."""
    exprs = {name: rules[name].get("expr", "") for name in sorted(UNTESTED_BARE) if name in rules}
    assert exprs, "no UNTESTED_BARE entry resolved to a rule — this gate is examining nothing"
    violations = bare_shape_violations(exprs)
    assert not violations, (
        "UNTESTED_BARE entries whose expression is not a plain comparison:\n  "
        + "\n  ".join(violations)
        + "\n\nMove them to UNTESTED_SHAPES_OWED, or give them a promtool case."
    )


def test_the_untested_groups_are_disjoint():
    """A name in two groups makes both claims, and dropping one leaves it exempt
    under the other without anyone noticing."""
    groups = {
        "UNTESTED_BARE": UNTESTED_BARE,
        "UNTESTED_SHAPES_OWED": UNTESTED_SHAPES_OWED,
        "UNTESTED_PLACEHOLDER_GATED": UNTESTED_PLACEHOLDER_GATED,
        "UNTESTED_LOGQL": UNTESTED_LOGQL,
    }
    overlaps = [
        f"{left} & {right}: {sorted(groups[left] & groups[right])}"
        for index, left in enumerate(groups)
        for right in list(groups)[index + 1 :]
        if groups[left] & groups[right]
    ]
    assert not overlaps, "UNTESTED groups overlap:\n  " + "\n  ".join(overlaps)


def test_the_shape_gate_reads_every_disqualifying_shape():
    """Mutation case: each shape the group claims to exclude must be reported,
    and a plain comparison must not be."""
    assert bare_shape_violations({"A": "up == 0"}) == []
    assert bare_shape_violations({"A": "(a / b) * 100 > 90"}) == []
    assert bare_shape_violations({"A": "time() - last_success > 3600"}) == []
    assert "a range window" in bare_shape_violations({"A": "delta(x[1h]) > 0"})[0]
    assert "a set operator" in bare_shape_violations({"A": "x > 0 unless y"})[0]
    assert "a function call" in bare_shape_violations({"A": "max by (job) (x) == 0"})[0]
    assert "a function call" in bare_shape_violations({"A": "absent(x)"})[0]


def test_the_gate_reports_an_alert_nothing_asserts_on():
    """Mutation case: an alert with no test and no exemption must be reported."""
    synthetic = {"SilentAlert"} | UNTESTED
    assert uncovered(synthetic, set()) == ["SilentAlert"]
    assert uncovered(synthetic, {"SilentAlert"}) == []


EXTRACTOR = REPO / "scripts" / "extract-prometheus-config.py"
RULES_DIR_SETTERS = ("Taskfile.yml", "taskfiles/lint.yml", ".gitlab-ci.yml")
_RULES_DIR_SET = re.compile(r"(?m)^\s*RULES_DIR\s*[:=]")


def rules_dir_wiring_owed(extractor: str, setters: dict[str, str]) -> bool:
    """True once the vendored extractor takes --rules-dir and no caller sets it."""
    if "--rules-dir" not in extractor:
        return False
    return not any(_RULES_DIR_SET.search(text) for text in setters.values())


def test_app_rule_extraction_is_wired_as_soon_as_the_extractor_supports_it():
    """The pinned extractor cannot read app PrometheusRule CRs, so RegistryCacheDown
    is unparsed by promtool. Only this tripwire forces the wiring when the pin moves."""
    assert EXTRACTOR.is_file(), "scripts/extract-prometheus-config.py is missing"
    setters = {
        name: (REPO / name).read_text(encoding="utf-8")
        for name in RULES_DIR_SETTERS
        if (REPO / name).is_file()
    }
    assert setters, "none of the Taskfile tree or .gitlab-ci.yml is present"
    assert not rules_dir_wiring_owed(EXTRACTOR.read_text(encoding="utf-8"), setters), (
        "the vendored extract-prometheus-config.py now accepts --rules-dir, so the "
        "app PrometheusRule CRs can be linted. Set RULES_DIR: kubernetes/apps in all "
        "three callers that already set RULE_TESTS_DIR: taskfiles/lint.yml's "
        "prometheus-config task, .gitlab-ci.yml's prometheus-config-lint variables, "
        "and tests/validate_render.py. In the same change add "
        "kubernetes/apps/**/prometheusrule.yaml to the prometheus_config_paths "
        "changes: anchor, drop 'App CRs are not extracted' from that job's header, "
        "and drop RegistryCacheDown with its comment group from UNTESTED once "
        "promtool parses the CR."
    )


def test_the_rules_dir_tripwire_reads_both_halves():
    """Mutation case: the option alone fires it, and REQUIRE_RULES_DIR must not
    read as a setter."""
    assert not rules_dir_wiring_owed("--release --am-config --dummy", {"Taskfile.yml": ""})
    assert rules_dir_wiring_owed("--rules-dir", {"Taskfile.yml": ""})
    assert rules_dir_wiring_owed("--rules-dir", {"Taskfile.yml": "REQUIRE_RULES_DIR: '1'"})
    assert not rules_dir_wiring_owed(
        "--rules-dir", {"Taskfile.yml": "  RULES_DIR: kubernetes/apps\n"}
    )
