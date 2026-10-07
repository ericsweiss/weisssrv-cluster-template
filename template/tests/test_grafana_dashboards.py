"""Static checks for the Grafana dashboards under kubernetes/infrastructure/observability.

Dashboards reach the cluster as opaque strings inside a configMapGenerator, so
`task flux:lint` validates the ConfigMap envelope and never parses the JSON.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
OBSERVABILITY = REPO / "kubernetes" / "infrastructure" / "observability"
DASHBOARDS = OBSERVABILITY / "dashboards"
KUSTOMIZATION = DASHBOARDS / "kustomization.yaml"
RELEASE = OBSERVABILITY / "kube-prometheus-stack" / "release.yaml"

pytestmark = pytest.mark.skipif(
    not DASHBOARDS.is_dir(), reason="this cluster ships no dashboards directory"
)

# Grafana's own datasource spellings, which no chart provisions.
BUILTIN_DATASOURCE_UIDS = {"grafana", "-- Grafana --", "-- Mixed --", "-- Dashboard --"}

# Folders the Grafana sidecar files dashboards into. Add one here when you add
# it to a configMapGenerator entry's grafana_folder annotation.
ALLOWED_FOLDERS = {"Infrastructure", "Networking", "Applications"}

# $__rate_interval and friends come from Grafana; ${cluster_*} is substituted by
# Flux from the cluster-config ConfigMap before the manifest reaches the cluster.
VARIABLE_PREFIX_ALLOWLIST = ("__", "cluster_")

_VARIABLE_RE = re.compile(r"\$(?:\{(\w+)[^}]*\}|(\w+))")
# A whole-value `$var` / `${var}` datasource: Grafana resolves it through a
# templating.list entry, so the variable has to exist.
_DATASOURCE_VARIABLE_RE = re.compile(r"^\$\{?([A-Za-z_]\w*)\}?$")
_QUERY_FIELDS = ("expr", "query", "legendFormat", "title", "expression")


def dashboard_paths() -> list[Path]:
    """Top-level dashboards plus any the generator registers from a subdirectory.

    A dashboard registered through kustomize's `key=path` form ships to Grafana
    like any other, so it is content-checked like any other.
    """
    found = {path.relative_to(DASHBOARDS).as_posix() for path in DASHBOARDS.glob("*.json")}
    if KUSTOMIZATION.is_file():
        found |= {
            name
            for name in generated_files(generator_entries(KUSTOMIZATION.read_text()))
            if (DASHBOARDS / name).is_file()
        }
    return sorted(DASHBOARDS / name for name in found)


def rel(path: Path) -> str:
    """The dashboard's path as the kustomization spells it, for a finding."""
    return path.relative_to(DASHBOARDS).as_posix() if path.is_relative_to(DASHBOARDS) else path.name


def load_dashboards() -> dict[Path, dict]:
    return {path: json.loads(path.read_text()) for path in dashboard_paths()}


def provisioned_datasource_uids(release_text: str) -> set[str]:
    """Datasource uids the kube-prometheus-stack release actually provisions."""
    release = yaml.safe_load(release_text)
    grafana = release["spec"]["values"]["grafana"]
    uids = {grafana["sidecar"]["datasources"]["uid"]}
    uids.update(source["uid"] for source in grafana.get("additionalDataSources", []))
    return uids | BUILTIN_DATASOURCE_UIDS


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def datasource_refs(dashboard: dict) -> set[tuple[str, str]]:
    """Every `datasource` reference, tagged by shape.

    `uid` a dict uid, `name` the legacy string form, `nouid` a dict with no uid.
    A `null` datasource is skipped: a row panel carries one.
    """
    refs: set[tuple[str, str]] = set()
    for node in _walk(dashboard):
        if "datasource" not in node:
            continue
        source = node["datasource"]
        if isinstance(source, dict):
            uid = source.get("uid")
            refs.add(("uid", uid) if isinstance(uid, str) else ("nouid", ""))
        elif isinstance(source, str):
            refs.add(("name", source))
    return refs


def datasource_findings(dashboard: dict, allowed: set[str]) -> list[str]:
    """Why each datasource reference would not resolve in provisioned Grafana."""
    declared = declared_variables(dashboard)
    findings = []
    for kind, value in sorted(datasource_refs(dashboard)):
        variable = _DATASOURCE_VARIABLE_RE.match(value)
        if kind == "nouid":
            findings.append("a datasource with no uid resolves to whichever one is default")
        elif kind == "name":
            if value in BUILTIN_DATASOURCE_UIDS or variable:
                continue
            findings.append(
                f"legacy string datasource {value!r}: provisioned Grafana matches by "
                "uid, so the panel falls back to the default datasource"
            )
        elif variable:
            # A surviving `${DS_*}` export placeholder has its own check.
            if value.startswith("${DS_") or variable.group(1).startswith("DS_"):
                continue
            if variable.group(1) not in declared:
                findings.append(
                    f"datasource variable {value} is declared in no templating.list "
                    f"entry; declared: {sorted(declared) or 'none'}"
                )
        elif value not in allowed:
            findings.append(
                f"datasource uid {value!r} is not provisioned; the cluster "
                f"provisions {sorted(allowed)}"
            )
    return findings


def declared_variables(dashboard: dict) -> set[str]:
    return {entry["name"] for entry in dashboard.get("templating", {}).get("list", [])}


def referenced_variables(dashboard: dict) -> set[str]:
    names = set()
    for node in _walk(dashboard):
        for field in _QUERY_FIELDS:
            value = node.get(field)
            if isinstance(value, str):
                names.update(braced or bare for braced, bare in _VARIABLE_RE.findall(value))
    return names


def undeclared_variables(dashboard: dict) -> set[str]:
    return {
        name
        for name in referenced_variables(dashboard) - declared_variables(dashboard)
        # $1 and friends are label_replace capture groups, not dashboard variables.
        if not name.startswith(VARIABLE_PREFIX_ALLOWLIST) and not name.isdigit()
    }


def generator_entries(kustomization_text: str) -> list[dict]:
    return yaml.safe_load(kustomization_text)["configMapGenerator"]


def generated_files(entries: list[dict]) -> list[str]:
    """The source path each `files:` entry registers, relative to the directory.

    kustomize allows `key=path`: the key names the ConfigMap entry and the path
    is the source file, which may sit in a subdirectory.
    """
    return [
        Path(os.path.normpath(str(name).split("=")[-1])).as_posix()
        for entry in entries
        for name in entry.get("files", [])
    ]


def sidecar_dashboard_convention(release_text: str) -> tuple[str, str]:
    """The sidecar's `label` and `labelValue`, the pair the kustomization must match.

    An empty `labelValue` is the chart's "any value" mode: the key alone is
    enough for discovery.
    """
    dashboards = yaml.safe_load(release_text)["spec"]["values"]["grafana"]["sidecar"][
        "dashboards"
    ]
    return dashboards["label"], str(dashboards.get("labelValue", "") or "")


def sidecar_label(kustomization: dict, entry: dict, label_key: str) -> str | None:
    """The discovery label, wherever it is set: per entry or hoisted."""
    per_entry = (entry.get("options") or {}).get("labels") or {}
    hoisted = (kustomization.get("generatorOptions") or {}).get("labels") or {}
    return per_entry.get(label_key) or hoisted.get(label_key)


def dashboard_folder(kustomization: dict, entry: dict) -> str | None:
    """The Grafana folder, wherever it is set: per entry or hoisted."""
    per_entry = (entry.get("options") or {}).get("annotations") or {}
    hoisted = (kustomization.get("generatorOptions") or {}).get("annotations") or {}
    return per_entry.get("grafana_folder") or hoisted.get("grafana_folder")


@pytest.fixture(scope="module")
def dashboards() -> dict[Path, dict]:
    return load_dashboards()


def test_dashboards_directory_is_not_empty() -> None:
    assert dashboard_paths(), f"no dashboards found under {DASHBOARDS}"


def test_every_dashboard_parses(dashboards) -> None:
    for path, dashboard in dashboards.items():
        assert isinstance(dashboard, dict), f"{rel(path)} is not a JSON object"
        assert dashboard.get("uid"), f"{rel(path)} has no uid"
        assert dashboard.get("title"), f"{rel(path)} has no title"


def test_uid_and_title_are_unique(dashboards) -> None:
    for field in ("uid", "title"):
        seen: dict[str, str] = {}
        for path, dashboard in dashboards.items():
            value = dashboard[field]
            assert value not in seen, (
                f"{rel(path)} reuses {field} {value!r}, already used by {seen[value]}"
            )
            seen[value] = rel(path)


def test_datasource_uids_are_provisioned(dashboards) -> None:
    allowed = provisioned_datasource_uids(RELEASE.read_text())
    for path, dashboard in dashboards.items():
        findings = datasource_findings(dashboard, allowed)
        assert not findings, f"{rel(path)}: " + "; ".join(findings)


def test_queries_only_use_declared_variables(dashboards) -> None:
    for path, dashboard in dashboards.items():
        undeclared = undeclared_variables(dashboard)
        assert not undeclared, (
            f"{rel(path)} references {sorted(undeclared)}, which templating.list does not declare"
        )


def test_generator_entries_and_files_are_one_to_one() -> None:
    listed = generated_files(generator_entries(KUSTOMIZATION.read_text()))
    assert len(listed) == len(set(listed)), "a dashboard is generated twice"
    registered = set(listed)
    absent = sorted(name for name in registered if not (DASHBOARDS / name).is_file())
    assert not absent, f"kustomization.yaml registers files that do not exist: {absent}"
    on_disk = {path.relative_to(DASHBOARDS).as_posix() for path in DASHBOARDS.glob("*.json")}
    orphans = sorted(on_disk - registered)
    assert not orphans, (
        f"on disk but in no configMapGenerator `files:` entry, so they never "
        f"reach the cluster: {orphans}"
    )


def test_generator_entries_carry_a_known_folder() -> None:
    kustomization = yaml.safe_load(KUSTOMIZATION.read_text())
    for entry in kustomization["configMapGenerator"]:
        folder = dashboard_folder(kustomization, entry)
        assert folder in ALLOWED_FOLDERS, (
            f"{entry['name']} files into {folder!r}; allowed folders are {sorted(ALLOWED_FOLDERS)}"
        )


def test_every_generated_configmap_carries_the_sidecar_label() -> None:
    """The sidecar discovers dashboards by label; losing it hides them.

    Both halves of the convention come from the release, so a cluster that sets
    its own label or labelValue is checked against its own spelling.
    """
    label_key, label_value = sidecar_dashboard_convention(RELEASE.read_text())
    kustomization = yaml.safe_load(KUSTOMIZATION.read_text())
    for entry in kustomization["configMapGenerator"]:
        found = sidecar_label(kustomization, entry, label_key)
        expected = f'{label_key}: "{label_value}"' if label_value else label_key
        assert found is not None and (not label_value or found == label_value), (
            f"{entry['name']} sets no {expected} label, so the Grafana "
            f"sidecar ignores the generated ConfigMap (found {found!r})"
        )


def test_no_grafana_com_import_placeholders() -> None:
    for path in dashboard_paths():
        text = path.read_text()
        for placeholder in ('"__inputs"', '"__requires"', "${DS_"):
            assert placeholder not in text, (
                f"{rel(path)} still carries the grafana.com import placeholder "
                f"{placeholder}; export with external sharing OFF and pin the "
                "datasource uid"
            )


# --- the checks above must be able to fail ----------------------------------


def _entry_name(spec: str) -> str:
    """The generated ConfigMap name for a `files:` entry, plain or `key=path`."""
    return "grafana-dashboard-%s" % Path(spec.split("=")[-1]).stem


def _kustomization(tmp_path: Path, files: list[str], folder: str = "Infrastructure",
                   label: str | None = "1", hoist: bool = True,
                   label_key: str = "grafana_dashboard") -> Path:
    """A minimal kustomization; hoist mirrors the shipped generatorOptions shape.

    A `files` entry may be a plain name or kustomize's `key=path` form.
    """
    generator_options: dict = {"disableNameSuffixHash": True}
    entry: dict = {}
    if hoist:
        generator_options["annotations"] = {"grafana_folder": folder}
        if label is not None:
            generator_options["labels"] = {label_key: label}
    else:
        options: dict = {"annotations": {"grafana_folder": folder}}
        if label is not None:
            options["labels"] = {label_key: label}
        entry["options"] = options
    path = tmp_path / "kustomization.yaml"
    path.write_text(yaml.safe_dump({
        "generatorOptions": generator_options,
        "configMapGenerator": [
            {"name": _entry_name(name),
             "files": [name],
             **entry}
            for name in files
        ],
    }))
    return path


def test_duplicate_uid_is_detected() -> None:
    dashboards = {Path("a.json"): {"uid": "same", "title": "A"},
                  Path("b.json"): {"uid": "same", "title": "B"}}
    with pytest.raises(AssertionError, match="reuses uid"):
        test_uid_and_title_are_unique(dashboards)


def test_unprovisioned_datasource_is_detected() -> None:
    dashboards = {
        Path("a.json"): {"panels": [{"datasource": {"type": "prometheus", "uid": "thanos"}}]}
    }
    with pytest.raises(AssertionError, match="thanos"):
        test_datasource_uids_are_provisioned(dashboards)


def test_undeclared_variable_is_detected() -> None:
    dashboard = {
        "templating": {"list": [{"name": "pod"}]},
        "panels": [{"targets": [{"expr": 'up{pod=~"$pod", server="$server"}'}]}],
    }
    assert undeclared_variables(dashboard) == {"server"}


def test_grafana_and_flux_variables_are_not_flagged() -> None:
    dashboard = {
        "templating": {"list": []},
        "panels": [{"targets": [
            {"expr": 'rate(up{job="x"}[$__rate_interval])'},
            {"expr": 'probe_success{instance="https://git.${cluster_internal_domain}"}'},
        ]}],
    }
    assert undeclared_variables(dashboard) == set()


def test_orphan_dashboard_file_is_detected(tmp_path, monkeypatch) -> None:
    (tmp_path / "kept.json").write_text("{}")
    (tmp_path / "orphan.json").write_text("{}")
    monkeypatch.setattr(
        "test_grafana_dashboards.KUSTOMIZATION", _kustomization(tmp_path, ["kept.json"])
    )
    monkeypatch.setattr("test_grafana_dashboards.DASHBOARDS", tmp_path)
    with pytest.raises(AssertionError, match="orphan.json"):
        test_generator_entries_and_files_are_one_to_one()


@pytest.mark.parametrize("hoist", [True, False])
def test_unknown_folder_is_detected(tmp_path, monkeypatch, hoist) -> None:
    monkeypatch.setattr(
        "test_grafana_dashboards.KUSTOMIZATION",
        _kustomization(tmp_path, ["kept.json"], folder="Sandbox", hoist=hoist),
    )
    with pytest.raises(AssertionError, match="Sandbox"):
        test_generator_entries_carry_a_known_folder()


@pytest.mark.parametrize("hoist", [True, False])
def test_missing_sidecar_label_is_detected(tmp_path, monkeypatch, hoist) -> None:
    monkeypatch.setattr(
        "test_grafana_dashboards.KUSTOMIZATION",
        _kustomization(tmp_path, ["kept.json"], label=None, hoist=hoist),
    )
    with pytest.raises(AssertionError, match="grafana_dashboard"):
        test_every_generated_configmap_carries_the_sidecar_label()


def _release(tmp_path: Path, label: str, label_value: str | None) -> Path:
    dashboards: dict = {"enabled": True, "label": label}
    if label_value is not None:
        dashboards["labelValue"] = label_value
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump({
        "spec": {"values": {"grafana": {"sidecar": {"dashboards": dashboards}}}}
    }))
    return path


def test_sidecar_convention_is_read_from_the_release(tmp_path, monkeypatch) -> None:
    """A cluster whose chart values name another label/labelValue pair passes."""
    monkeypatch.setattr(
        "test_grafana_dashboards.RELEASE", _release(tmp_path, "dashboards.grafana", "true")
    )
    monkeypatch.setattr(
        "test_grafana_dashboards.KUSTOMIZATION",
        _kustomization(tmp_path, ["kept.json"], label="true", label_key="dashboards.grafana"),
    )
    test_every_generated_configmap_carries_the_sidecar_label()


def test_sidecar_label_value_mismatch_is_detected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "test_grafana_dashboards.RELEASE", _release(tmp_path, "grafana_dashboard", "true")
    )
    monkeypatch.setattr(
        "test_grafana_dashboards.KUSTOMIZATION", _kustomization(tmp_path, ["kept.json"], label="1")
    )
    with pytest.raises(AssertionError, match="found '1'"):
        test_every_generated_configmap_carries_the_sidecar_label()


def test_an_empty_label_value_accepts_any_value(tmp_path, monkeypatch) -> None:
    """The chart's "any value" mode: the key alone is the whole convention."""
    monkeypatch.setattr(
        "test_grafana_dashboards.RELEASE", _release(tmp_path, "grafana_dashboard", None)
    )
    monkeypatch.setattr(
        "test_grafana_dashboards.KUSTOMIZATION",
        _kustomization(tmp_path, ["kept.json"], label="anything"),
    )
    test_every_generated_configmap_carries_the_sidecar_label()


def test_a_registered_file_that_does_not_exist_is_detected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "test_grafana_dashboards.KUSTOMIZATION", _kustomization(tmp_path, ["gone.json"])
    )
    monkeypatch.setattr("test_grafana_dashboards.DASHBOARDS", tmp_path)
    with pytest.raises(AssertionError, match="gone.json"):
        test_generator_entries_and_files_are_one_to_one()


def test_a_subdirectory_dashboard_is_collected_and_checked(tmp_path, monkeypatch) -> None:
    """A `key=path` entry ships a dashboard from a subdirectory, so the content
    checks have to reach it: otherwise it reaches Grafana unvalidated."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "dash.json").write_text(
        json.dumps({
            "uid": "sub", "title": "Sub",
            "panels": [{"datasource": {"type": "prometheus", "uid": "thanos"}}],
        })
    )
    monkeypatch.setattr(
        "test_grafana_dashboards.KUSTOMIZATION",
        _kustomization(tmp_path, ["dash.json=sub/dash.json"]),
    )
    monkeypatch.setattr("test_grafana_dashboards.DASHBOARDS", tmp_path)
    assert [rel(path) for path in dashboard_paths()] == ["sub/dash.json"]
    test_generator_entries_and_files_are_one_to_one()
    with pytest.raises(AssertionError, match="sub/dash.json: datasource uid 'thanos'"):
        test_datasource_uids_are_provisioned(load_dashboards())


def test_a_legacy_string_datasource_is_detected() -> None:
    allowed = {"prometheus"} | BUILTIN_DATASOURCE_UIDS
    findings = datasource_findings({"panels": [{"datasource": "Prometheus"}]}, allowed)
    assert findings and "legacy string datasource" in findings[0]
    assert datasource_findings({"panels": [{"datasource": "-- Mixed --"}]}, allowed) == []


def test_a_datasource_with_no_uid_is_detected() -> None:
    allowed = {"prometheus"} | BUILTIN_DATASOURCE_UIDS
    findings = datasource_findings(
        {"panels": [{"datasource": {"type": "prometheus"}}]}, allowed
    )
    assert findings and "no uid" in findings[0]
    assert datasource_findings({"rows": [{"datasource": None}]}, allowed) == []


def test_an_undeclared_datasource_variable_is_detected() -> None:
    """A `$var` uid is only as good as the templating.list entry behind it."""
    allowed = {"prometheus"} | BUILTIN_DATASOURCE_UIDS
    panels = [{"datasource": {"type": "prometheus", "uid": "${ds}"}}]
    findings = datasource_findings({"panels": panels}, allowed)
    assert findings and "datasource variable ${ds}" in findings[0]
    declared = {"templating": {"list": [{"name": "ds"}]}, "panels": panels}
    assert datasource_findings(declared, allowed) == []
