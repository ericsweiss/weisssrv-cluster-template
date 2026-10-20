"""Coverage for check-tenant-wiring.py.

No tenant is onboarded, so the live tree exercises only the empty case. Every
arm runs against a fixture tenant file, and each mutation case must FAIL.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml
from conftest import REPO, load_script

gate = load_script("check-tenant-wiring.py")

TENANT = textwrap.dedent(
    """\
    ---
    apiVersion: v1
    kind: Namespace
    metadata:
      name: demo-app
      annotations:
        kustomize.toolkit.fluxcd.io/prune: disabled
      labels:
        fluxcd.io/tenant: demo-app
        pod-security.kubernetes.io/enforce: baseline
        pod-security.kubernetes.io/warn: restricted
        pod-security.kubernetes.io/audit: restricted
    ---
    apiVersion: v1
    kind: ResourceQuota
    metadata:
      name: demo-app-quota
      namespace: demo-app
      annotations:
        kustomize.toolkit.fluxcd.io/prune: disabled
    spec:
      hard:
        pods: "10"
        requests.ephemeral-storage: 4Gi
        limits.ephemeral-storage: 8Gi
        persistentvolumeclaims: "4"
        tenant-local.storageclass.storage.k8s.io/requests.storage: 40Gi
    ---
    apiVersion: v1
    kind: LimitRange
    metadata:
      name: demo-app-limits
      namespace: demo-app
      annotations:
        kustomize.toolkit.fluxcd.io/prune: disabled
    spec:
      limits:
        - type: Container
          default:
            memory: 512Mi
            ephemeral-storage: 512Mi
          max:
            ephemeral-storage: 2Gi
    ---
    apiVersion: v1
    kind: ServiceAccount
    metadata:
      name: demo-app-flux
      namespace: flux-system
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: RoleBinding
    metadata:
      name: demo-app-flux-admin
      namespace: demo-app
    subjects:
      - kind: ServiceAccount
        name: demo-app-flux
        namespace: flux-system
    roleRef:
      kind: ClusterRole
      name: admin
      apiGroup: rbac.authorization.k8s.io
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: RoleBinding
    metadata:
      name: demo-app-flux-crd-editor
      namespace: demo-app
    subjects:
      - kind: ServiceAccount
        name: demo-app-flux
        namespace: flux-system
    roleRef:
      kind: ClusterRole
      name: tenant-crd-editor
      apiGroup: rbac.authorization.k8s.io
    ---
    apiVersion: source.toolkit.fluxcd.io/v1
    kind: GitRepository
    metadata:
      name: demo-app
      namespace: flux-system
    spec:
      interval: 1m
      url: https://example.invalid/demo/demo-app
      ref:
        branch: main
    ---
    apiVersion: kustomize.toolkit.fluxcd.io/v1
    kind: Kustomization
    metadata:
      name: demo-app
      namespace: flux-system
    spec:
      dependsOn:
        - name: infrastructure-configs
      serviceAccountName: demo-app-flux
      sourceRef:
        kind: GitRepository
        name: demo-app
      path: ./kubernetes/flux
      prune: true
      targetNamespace: demo-app
      postBuild:
        substituteFrom:
          - kind: ConfigMap
            name: cluster-versions
            optional: false
          - kind: ConfigMap
            name: cluster-config
            optional: false
    """
)

STORE = textwrap.dedent(
    """\
    ---
    apiVersion: external-secrets.io/v1
    kind: ClusterSecretStore
    metadata:
      name: onepassword-demo-app
    spec:
      conditions:
        - namespaces:
            - demo-app
      provider:
        fake:
          data: []
    """
)

AGGREGATOR = textwrap.dedent(
    """\
    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    resources:
      - tenant-crd-editor.yaml
      - demo-app.yaml
    """
)


def write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path, f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}", AGGREGATOR)
    write(tmp_path, f"{gate.TENANTS_DIR}/tenant-crd-editor.yaml", "kind: ClusterRole\n")
    write(tmp_path, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT)
    return tmp_path


def drop(body: str, needle: str) -> str:
    return "\n".join(line for line in body.splitlines() if needle not in line) + "\n"


def test_a_complete_tenant_file_passes(repo: Path) -> None:
    assert gate.check(repo) == []
    assert gate.main(["--repo-root", str(repo)]) == 0


@pytest.mark.parametrize(
    ("needle", "expected"),
    [
        ("serviceAccountName", "serviceAccountName"),
        ("prune: true", "prune: true"),
        ("targetNamespace", "targetNamespace"),
        ("infrastructure-configs", "dependOn"),
        ("pod-security.kubernetes.io/enforce", "pod-security.kubernetes.io/enforce"),
        ("fluxcd.io/tenant", "fluxcd.io/tenant"),
    ],
)
def test_a_missing_line_fails(repo: Path, needle: str, expected: str) -> None:
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", drop(TENANT, needle))
    problems = gate.check(repo)
    assert any(expected in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


@pytest.mark.parametrize("role", ["admin", "tenant-crd-editor"])
def test_a_missing_rolebinding_fails(repo: Path, role: str) -> None:
    body = TENANT.replace(f"  name: {role}\n  apiGroup:", "  name: view\n  apiGroup:")
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    assert any(f"grants {role}" in p for p in gate.check(repo))


@pytest.mark.parametrize("source", list(gate.REQUIRED_SUBSTITUTE_SOURCES))
def test_a_missing_substitute_source_fails(repo: Path, source: str) -> None:
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", drop(TENANT, f"name: {source}"))
    problems = gate.check(repo)
    assert any(f"substituteFrom ConfigMap {source}" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_an_optional_substitute_source_fails(repo: Path) -> None:
    body = TENANT.replace("        optional: false", "        optional: true")
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    assert any("is not optional: false" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_kustomization_without_postbuild_fails(repo: Path) -> None:
    body = TENANT.split("  postBuild:")[0]
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    assert len([p for p in problems if "substituteFrom" in p]) == len(
        gate.REQUIRED_SUBSTITUTE_SOURCES
    ), problems


def test_a_tenant_that_pins_its_own_versions_needs_only_cluster_config(repo: Path) -> None:
    """A tenant references no ${*_version}, so cluster-versions is optional."""
    body = TENANT.replace(
        "      - kind: ConfigMap\n        name: cluster-versions\n"
        "        optional: false\n",
        "",
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    assert gate.check(repo) == []


def test_a_kustomization_without_a_source_fails(repo: Path) -> None:
    """Mutation case: a tenant pointed at no source reconciles nothing."""
    body = TENANT.replace(
        "  sourceRef:\n    kind: GitRepository\n    name: demo-app\n", ""
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    assert any("no sourceRef.name" in p for p in gate.check(repo))


def test_a_source_the_file_does_not_declare_fails(repo: Path) -> None:
    body = TENANT.replace("    name: demo-app\n  path:", "    name: other\n  path:")
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    assert any("GitRepository other" in p for p in gate.check(repo))


def test_a_kustomization_without_a_path_fails(repo: Path) -> None:
    body = TENANT.replace("  path: ./kubernetes/flux\n", "")
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    assert any("no spec.path" in p for p in gate.check(repo))


def test_a_path_naming_this_repos_own_tree_fails(repo: Path) -> None:
    """Mutation case: the tenant account would reconcile the cluster's manifests."""
    body = TENANT.replace("  path: ./kubernetes/flux\n", "  path: ./kubernetes\n")
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    assert any("directory of this" in p for p in gate.check(repo))


@pytest.mark.parametrize("kind", list(gate.REQUIRED_CAPS))
def test_a_missing_namespace_cap_fails(repo: Path, kind: str) -> None:
    """`admin` places no ceiling on requests, so both caps are wiring."""
    body = "\n---\n".join(
        d for d in TENANT.split("\n---\n") if f"kind: {kind}\n" not in d
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    assert any(f"declares no {kind}" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


# The tenant-local StorageClass ships only under tenant_storage; the gate
# demands the claim caps exactly when it does.
TENANT_LOCAL = (
    REPO / "kubernetes" / "infrastructure" / "controllers" / "local-path-provisioner"
    / "storageclass.yaml"
)
CLAIM_CAPS = (
    "persistentvolumeclaims",
    "tenant-local.storageclass.storage.k8s.io/requests.storage",
)


def test_the_claim_caps_are_required_exactly_when_tenant_local_ships() -> None:
    assert gate.STORAGE_QUOTA_KEYS == (CLAIM_CAPS if TENANT_LOCAL.is_file() else ())


@pytest.mark.parametrize(
    "missing",
    [CLAIM_CAPS[:1], CLAIM_CAPS[1:], CLAIM_CAPS],
    ids=["persistentvolumeclaims", "requests.storage", "both"],
)
def test_a_quota_that_does_not_cap_tenant_local_claims_fails(
    repo: Path, missing: tuple[str, ...]
) -> None:
    """tenant-local sizes are not enforced, so the quota is the only bound on
    how many claims, and how much storage, a tenant may declare."""
    body = TENANT
    for key in missing:
        body = drop(body, f"    {key}:")
        assert key not in body
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    if not TENANT_LOCAL.is_file():
        assert problems == []
        return
    expected = (
        "ResourceQuota does not cap tenant-local claims; missing spec.hard keys "
        + ", ".join(missing)
    )
    assert any(expected in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_the_claim_caps_may_sit_on_a_second_quota(repo: Path) -> None:
    """The keys are read across every ResourceQuota the tenant file declares."""
    body = drop(TENANT, "    tenant-local.storageclass")
    assert "tenant-local" not in body
    body += textwrap.dedent(
        """\
        ---
        apiVersion: v1
        kind: ResourceQuota
        metadata:
          name: demo-app-storage
          namespace: demo-app
          annotations:
            kustomize.toolkit.fluxcd.io/prune: disabled
        spec:
          hard:
            tenant-local.storageclass.storage.k8s.io/requests.storage: 40Gi
        """
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    assert gate.check(repo) == []


@pytest.mark.parametrize("key", list(gate.EPHEMERAL_QUOTA_KEYS))
def test_a_quota_that_does_not_cap_ephemeral_storage_fails(repo: Path, key: str) -> None:
    """emptyDir and writable layers draw on the node's disk, which nothing but
    the quota bounds for the namespace as a whole."""
    body = drop(TENANT, f"    {key}:")
    assert key not in body
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    expected = f"ResourceQuota does not cap ephemeral storage; missing spec.hard keys {key}"
    assert any(expected in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


# The fixture's Container ephemeral-storage lines, by the LimitRange key they sit under.
EPHEMERAL_LIMITS = {
    "default": "        ephemeral-storage: 512Mi",
    "max": "        ephemeral-storage: 2Gi",
}


@pytest.mark.parametrize(
    "unset", [("default",), ("max",), ("default", "max")], ids=["default", "max", "both"]
)
def test_a_limit_range_without_an_ephemeral_storage_limit_fails(
    repo: Path, unset: tuple[str, ...]
) -> None:
    """A tenant container with no ephemeral-storage limit could spend the node's
    disk unbounded, so the LimitRange supplies the default and the ceiling."""
    body = TENANT
    for key in unset:
        assert body.count(EPHEMERAL_LIMITS[key] + "\n") == 1, key
        body = body.replace(EPHEMERAL_LIMITS[key] + "\n", "")
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    expected = (
        f"LimitRange demo-app-limits sets no ephemeral-storage under {', '.join(unset)}"
    )
    assert any(expected in p for p in problems), problems
    assert len(problems) == 1, problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_limit_range_without_a_container_entry_fails(repo: Path) -> None:
    """A Pod- or PVC-only LimitRange supplies none of the per-container defaults
    and ceilings, so a container with no resources block lands unbounded."""
    assert TENANT.count("    - type: Container\n") == 1
    body = TENANT.replace("    - type: Container\n", "    - type: Pod\n", 1)
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    assert any("LimitRange demo-app-limits has no Container entry" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_cap_in_another_namespace_fails(repo: Path) -> None:
    body = TENANT.replace("  name: demo-app-quota\n  namespace: demo-app",
                          "  name: demo-app-quota\n  namespace: other-app")
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    assert any("neither the tenant namespace demo-app" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_namespaced_document_without_a_namespace_fails(repo: Path) -> None:
    """The root kustomization sets no targetNamespace, so it would land in default."""
    body = TENANT.replace("  name: demo-app-limits\n  namespace: demo-app",
                          "  name: demo-app-limits")
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    assert any("names no metadata.namespace" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_an_unlisted_tenant_file_fails(repo: Path) -> None:
    write(
        repo,
        f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}",
        AGGREGATOR.replace("  - demo-app.yaml\n", ""),
    )
    assert any("not listed" in p for p in gate.check(repo))


def test_a_listed_yml_tenant_is_inspected(repo: Path) -> None:
    """Kustomize applies any suffix, so a *.yml tenant must not escape the gate."""
    (repo / gate.TENANTS_DIR / "demo-app.yaml").unlink()
    write(
        repo,
        f"{gate.TENANTS_DIR}/demo-app.yml",
        drop(TENANT, "pod-security.kubernetes.io/enforce"),
    )
    write(
        repo,
        f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}",
        AGGREGATOR.replace("  - demo-app.yaml\n", "  - demo-app.yml\n"),
    )
    problems = gate.check(repo)
    assert any("pod-security.kubernetes.io/enforce" in p for p in problems), problems
    assert not any("not listed" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_dot_prefixed_resource_is_the_same_file(repo: Path) -> None:
    """kustomize accepts `./demo-app.yaml`, so the gate must not call it unlisted."""
    write(
        repo,
        f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}",
        AGGREGATOR.replace("  - demo-app.yaml\n", "  - ./demo-app.yaml\n"),
    )
    problems = gate.check(repo)
    assert not any("not listed" in p or "does not exist" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 0


def test_a_listed_but_absent_file_fails(repo: Path) -> None:
    (repo / gate.TENANTS_DIR / "demo-app.yaml").unlink()
    assert any("does not exist" in p for p in gate.check(repo))


def test_a_service_account_in_the_tenant_namespace_fails(repo: Path) -> None:
    body = TENANT.replace(
        "kind: ServiceAccount\nmetadata:\n  name: demo-app-flux\n  namespace: flux-system",
        "kind: ServiceAccount\nmetadata:\n  name: demo-app-flux\n  namespace: demo-app",
        1,
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    assert any("kustomize-controller impersonates" in p for p in gate.check(repo))


def test_a_tenant_owned_store_scoped_to_its_namespace_passes(repo: Path) -> None:
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT + STORE)
    assert gate.check(repo) == []


def test_a_store_without_conditions_fails(repo: Path) -> None:
    """The corpus gates never reach this tree, so an unconditioned tenant store
    is caught here or nowhere, and it is readable from every namespace."""
    body = TENANT + STORE.replace(
        "  conditions:\n    - namespaces:\n        - demo-app\n", ""
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    assert any("has no spec.conditions" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_store_that_does_not_admit_the_tenant_namespace_fails(repo: Path) -> None:
    write(
        repo,
        f"{gate.TENANTS_DIR}/demo-app.yaml",
        TENANT + STORE.replace("        - demo-app", "        - other-app"),
    )
    problems = gate.check(repo)
    assert any("do not admit the tenant namespace" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_store_reaching_a_second_namespace_fails(repo: Path) -> None:
    write(
        repo,
        f"{gate.TENANTS_DIR}/demo-app.yaml",
        TENANT + STORE.replace("        - demo-app", "        - demo-app\n        - other-app"),
    )
    problems = gate.check(repo)
    assert any("reaches past the tenant namespace" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_store_with_a_namespace_selector_fails(repo: Path) -> None:
    """A selector admits whatever carries the label, so it is not a namespace scope."""
    body = TENANT + STORE.replace(
        "    - namespaces:\n        - demo-app",
        "    - namespaces:\n        - demo-app\n    - namespaceSelector:\n"
        "        matchLabels:\n          tenant: \"true\"",
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", body)
    problems = gate.check(repo)
    assert any("namespaceSelector" in p for p in problems), problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_an_empty_tree_the_aggregator_agrees_with_passes(tmp_path: Path) -> None:
    """The shipped state: nothing onboarded, so the gate says so instead of
    reporting OK on zero inspected files."""
    write(
        tmp_path,
        f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}",
        "resources:\n  - tenant-crd-editor.yaml\n",
    )
    write(tmp_path, f"{gate.TENANTS_DIR}/tenant-crd-editor.yaml", "kind: ClusterRole\n")
    assert gate.main(["--repo-root", str(tmp_path)]) == 0


def test_an_empty_tree_that_lists_a_tenant_is_vacuous(tmp_path: Path) -> None:
    write(tmp_path, f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}", AGGREGATOR)
    write(tmp_path, f"{gate.TENANTS_DIR}/tenant-crd-editor.yaml", "kind: ClusterRole\n")
    assert gate.main(["--repo-root", str(tmp_path)]) == 2


def test_a_missing_tenants_directory_is_vacuous(tmp_path: Path) -> None:
    with pytest.raises(gate.Vacuous):
        gate.check(tmp_path)
    assert gate.main(["--repo-root", str(tmp_path)]) == 2


def test_the_live_tree_is_clean() -> None:
    assert gate.check(REPO) == []


def test_the_readme_example_satisfies_the_gate(tmp_path: Path) -> None:
    """The onboarding example is what a tenant copies, so it must pass the gate."""
    readme = (REPO / gate.TENANTS_DIR / "README.md").read_text()
    blocks = re.findall(r"```yaml\n(.*?)```", readme, re.S)
    example = next(
        b for b in blocks if "kind: Kustomization" in b and "serviceAccountName" in b
    )
    docs = [d for d in yaml.safe_load_all(example) if isinstance(d, dict)]
    namespace = next(d for d in docs if d.get("kind") == "Namespace")["metadata"]["name"]
    write(tmp_path, f"{gate.TENANTS_DIR}/{namespace}.yaml", example)
    write(
        tmp_path,
        f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}",
        f"resources:\n  - tenant-crd-editor.yaml\n  - {namespace}.yaml\n",
    )
    assert gate.check(tmp_path) == []


def test_an_unparseable_aggregator_exits_2(repo: Path, capsys) -> None:
    """A malformed aggregator is an operator error: the subject scan reads it."""
    write(repo, f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}", "resources: [\n")
    assert gate.main(["--repo-root", str(repo)]) == 2
    assert gate.AGGREGATOR in capsys.readouterr().err


def test_an_aggregator_that_is_not_a_mapping_exits_2(repo: Path) -> None:
    write(repo, f"{gate.TENANTS_DIR}/{gate.AGGREGATOR}", "- tenant-crd-editor.yaml\n")
    assert gate.main(["--repo-root", str(repo)]) == 2


def test_a_non_utf8_aggregator_exits_2(repo: Path) -> None:
    (repo / gate.TENANTS_DIR / gate.AGGREGATOR).write_bytes(b"resources:\n  - \xff\xfe.yaml\n")
    assert gate.main(["--repo-root", str(repo)]) == 2


def test_a_non_utf8_tenant_file_is_reported_not_dropped(repo: Path) -> None:
    (repo / gate.TENANTS_DIR / "demo-app.yaml").write_bytes(b"kind: \xff\xfe\n")
    problems = gate.check(repo)
    assert any("demo-app.yaml: unparseable YAML" in p for p in problems)


SECOND = textwrap.dedent(
    """\
    ---
    apiVersion: kustomize.toolkit.fluxcd.io/v1
    kind: Kustomization
    metadata:
      name: demo-app-db
      namespace: flux-system
    spec:
      dependsOn:
        - name: infrastructure-configs
      sourceRef:
        kind: GitRepository
        name: demo-app
      path: ./kubernetes/db
      prune: true
      targetNamespace: demo-app
      postBuild:
        substituteFrom:
          - kind: ConfigMap
            name: cluster-config
            optional: false
    """
)


def test_every_kustomization_in_a_tenant_file_is_checked(repo: Path) -> None:
    """A second Kustomization reconciles with the first one's reach, so it is
    held to the same confinement rather than skipped."""
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT + SECOND)
    problems = gate.check(repo)
    assert any("demo-app-db" in p and "serviceAccountName" in p for p in problems), problems


CONFINED_SECOND = SECOND.replace(
    "  dependsOn:\n", "  serviceAccountName: demo-app-flux\n  dependsOn:\n"
).replace("  prune: true\n", "  prune: true\n  deletionPolicy: Orphan\n")
GUARD = "  annotations:\n    kustomize.toolkit.fluxcd.io/prune: disabled\n"


def test_a_confined_second_kustomization_passes(repo: Path) -> None:
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT + CONFINED_SECOND)
    assert gate.check(repo) == []


def _unguarded(kind: str, value: str | None) -> str:
    """TENANT with `kind`'s prune guard removed (None) or set to `value`."""
    docs = TENANT.split("\n---\n")
    hit = [i for i, d in enumerate(docs) if f"kind: {kind}\n" in d]
    assert len(hit) == 1 and GUARD in docs[hit[0]]
    docs[hit[0]] = docs[hit[0]].replace(
        GUARD, "" if value is None else GUARD.replace("disabled", value)
    )
    return "\n---\n".join(docs)


@pytest.mark.parametrize("kind", ["Namespace", "ResourceQuota", "LimitRange"])
@pytest.mark.parametrize("value", [None, "enabled"], ids=["absent", "enabled"])
@pytest.mark.parametrize("second", ["", CONFINED_SECOND], ids=["app-only", "database"])
def test_a_tenant_object_without_the_prune_guard_fails(
    repo: Path, kind: str, value: str | None, second: str
) -> None:
    """Every tenant's Namespace and caps outlive its wiring: a pruned Namespace
    cascades through every object and PVC in it, which `deletionPolicy: Orphan`
    cannot stop, and a pruned cap leaves the surviving namespace unbounded."""
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", _unguarded(kind, value) + second)
    problems = gate.check(repo)
    assert any(
        kind in p and "kustomize.toolkit.fluxcd.io/prune: disabled" in p
        for p in problems
    ), problems
    assert len(problems) == 1, problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_a_database_kustomization_that_orphans_on_deletion_passes(repo: Path) -> None:
    """The database Kustomization carries `deletionPolicy: Orphan`, so deleting its
    wiring leaves the database whole; the gate must accept the field."""
    assert "deletionPolicy: Orphan" in CONFINED_SECOND
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT + CONFINED_SECOND)
    assert gate.check(repo) == []


@pytest.mark.parametrize(
    "policy", [None, "Delete", "MirrorPrune", "WaitForTermination"],
    ids=["absent", "delete", "mirror-prune", "wait"],
)
@pytest.mark.parametrize("by", ["name", "path", "nested-path"])
def test_a_database_kustomization_without_orphan_fails(
    repo: Path, policy: str | None, by: str
) -> None:
    """Without `deletionPolicy: Orphan`, deleting the db Kustomization garbage
    collects the database Cluster and its PVCs with it."""
    replacement = "" if policy is None else f"  deletionPolicy: {policy}\n"
    second = CONFINED_SECOND.replace("  deletionPolicy: Orphan\n", replacement)
    if by == "path":
        second = second.replace("name: demo-app-db\n", "name: demo-app-data\n")
    if by == "nested-path":
        second = second.replace("name: demo-app-db\n", "name: demo-app-data\n").replace(
            "path: ./kubernetes/db\n", "path: ./kubernetes/db/overlays/prod\n"
        )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT + second)
    problems = gate.check(repo)
    assert len(problems) == 1, problems
    assert "deletionPolicy: Orphan" in problems[0], problems
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_an_app_kustomization_needs_no_orphan(repo: Path) -> None:
    """Only the database tree is held to Orphan; the app tree prunes normally."""
    assert "deletionPolicy" not in TENANT
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT)
    assert gate.check(repo) == []


ROUTING = textwrap.dedent(
    """\
    ---
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: tenant-routing-demo-app
      namespace: flux-system
      annotations:
        kustomize.toolkit.fluxcd.io/prune: disabled
    data:
      hosts: |
        demo-app.example.com
    ---
    apiVersion: admissionregistration.k8s.io/v1
    kind: ValidatingAdmissionPolicyBinding
    metadata:
      name: tenant-traefik-hostnames-demo-app
      annotations:
        kustomize.toolkit.fluxcd.io/prune: disabled
    spec:
      policyName: tenant-traefik-hostnames
      validationActions: [Deny]
      paramRef:
        name: tenant-routing-demo-app
        namespace: flux-system
        parameterNotFoundAction: Deny
      matchResources:
        namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: demo-app
    ---
    apiVersion: admissionregistration.k8s.io/v1
    kind: ValidatingAdmissionPolicyBinding
    metadata:
      name: tenant-certificates-demo-app
      annotations:
        kustomize.toolkit.fluxcd.io/prune: disabled
    spec:
      policyName: tenant-certificates
      validationActions: [Deny]
      paramRef:
        name: tenant-routing-demo-app
        namespace: flux-system
        parameterNotFoundAction: Deny
      matchResources:
        namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: demo-app
    """
)


def test_the_hostname_binding_is_cluster_scoped(repo: Path) -> None:
    """The per-tenant hostname and certificate bindings name no namespace
    because they have none."""
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT + ROUTING)
    assert gate.check(repo) == []


@pytest.mark.parametrize(
    "name",
    [
        "tenant-routing-demo-app",
        "tenant-traefik-hostnames-demo-app",
        "tenant-certificates-demo-app",
    ],
)
@pytest.mark.parametrize("value", [None, "enabled"], ids=["absent", "enabled"])
def test_a_hostname_grant_or_binding_without_the_prune_guard_fails(
    repo: Path, name: str, value: str | None
) -> None:
    """The Namespace outlives the wiring, so the grant and both bindings that
    admit writes in it must too, until the Namespace is deleted by hand."""
    docs = ROUTING.split("\n---\n")
    hit = [i for i, d in enumerate(docs) if f"metadata:\n  name: {name}\n" in d]
    assert len(hit) == 1 and GUARD in docs[hit[0]]
    docs[hit[0]] = docs[hit[0]].replace(
        GUARD, "" if value is None else GUARD.replace("disabled", value)
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT + "\n---\n".join(docs))
    problems = gate.check(repo)
    assert len(problems) == 1, problems
    assert name in problems[0] and "kustomize.toolkit.fluxcd.io/prune: disabled" in problems[0]
    assert gate.main(["--repo-root", str(repo)]) == 1


def test_an_unrelated_configmap_or_binding_needs_no_prune_guard(repo: Path) -> None:
    """Only the hostname grant and its two bindings are held to the guard."""
    other = (
        ROUTING.replace(GUARD, "")
        .replace("tenant-routing-demo-app", "demo-app-settings")
        .replace("policyName: tenant-traefik-hostnames", "policyName: demo-app-other")
        .replace("policyName: tenant-certificates", "policyName: demo-app-another")
    )
    write(repo, f"{gate.TENANTS_DIR}/demo-app.yaml", TENANT + other)
    assert gate.check(repo) == []
