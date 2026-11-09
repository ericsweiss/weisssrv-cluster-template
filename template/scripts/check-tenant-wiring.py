#!/usr/bin/env python3
"""Assert every tenant wiring file carries its confinement; each omitted piece fails
OPEN: PSS labels, caps, SA and RoleBindings, stores and their vault, landing and
platform-owned namespaces, prune guards and the db Orphan policy. Exit 0/1/2."""
from __future__ import annotations

import argparse
import posixpath
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this script's own directory)
        NAME_LABEL,
        OperatorError,
        api_group,
        condition_admits,
        load_docs,
    )
    from tenant_common import (  # noqa: E402  (resolved from this script's own directory)
        AGGREGATOR,
        FLUX_NAMESPACE,
        GRANT_POLICIES,
        GRANT_PREFIX,
        SHARED,
        TENANT_LABEL,
        TENANT_STORAGE_CLASS,
        Targets,
        add_target_arguments,
        as_mapping,
        listed_resources,
        load_mappings,
        metadata,
        of_kind,
        resolve_targets,
        routing_binding_spec,
    )
except ImportError as exc:  # pragma: no cover - environment guard
    print(
        f"ERROR: {exc}: tenant_common.py and gate_common.py must be vendored "
        "beside this gate (see scripts/vendorable-paths.yml)",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

STORE_KIND = "ClusterSecretStore"
FLUX_KUSTOMIZE_GROUP = "kustomize.toolkit.fluxcd.io"
# The providers whose vault reach this gate can read. Any other provider on a
# tenant store is a finding, as its reach cannot be proven to stop at the tenant.
VAULT_PROVIDERS = ("onepassword", "onepasswordSDK")
# Providers that read only what the tenant's own token reaches, so the token's
# namespace is the whole check.
TOKEN_PROVIDERS = ("gitlab",)
STORE_PROVIDERS = (*VAULT_PROVIDERS, *TOKEN_PROVIDERS)
# The apiserver creates these, so no manifest declares them and a tenant never owns one.
BUILTIN_NAMESPACES = frozenset({"default", "kube-system", "kube-public", "kube-node-lease"})
PLATFORM_TREE = "kubernetes"
REQUIRED_DEPENDENCY = "infrastructure-configs"
SOURCE_KIND = "GitRepository"
REQUIRED_ROLES = ("admin", "tenant-crd-editor")
RBAC_GROUP = "rbac.authorization.k8s.io"
# What a tenant may read in flux-system, by API group: (resource, kind) of its own objects.
READABLE_FLUX_OBJECTS = {
    FLUX_KUSTOMIZE_GROUP: ("kustomizations", "Kustomization"),
    "source.toolkit.fluxcd.io": ("gitrepositories", SOURCE_KIND),
}
READ_VERBS = frozenset({"get", "list", "watch"})
# A tenant pins its own image versions, so only cluster identity is required.
REQUIRED_SUBSTITUTE_SOURCES = ("cluster-config",)
ALLOWED_SUBSTITUTE_SOURCES = (*REQUIRED_SUBSTITUTE_SOURCES, "cluster-versions")
# The tenant contract's Pod Security levels: baseline enforced, restricted advised.
PSS_PREFIX = "pod-security.kubernetes.io/"
PSS_LEVELS = {f"{PSS_PREFIX}enforce": "baseline", f"{PSS_PREFIX}warn": "restricted",
              f"{PSS_PREFIX}audit": "restricted"}
REQUIRED_NS_LABELS = (
    TENANT_LABEL,
    "pod-security.kubernetes.io/enforce",
    "pod-security.kubernetes.io/warn",
    "pod-security.kubernetes.io/audit",
)
# The tenant's `admin` RoleBinding places no ceiling on what it may request.
REQUIRED_CAPS = ("ResourceQuota", "LimitRange")
CLAIM_COUNT_KEY = "persistentvolumeclaims"
# emptyDir and container writable layers draw on the node's disk; only the
# quota and the LimitRange bound them.
EPHEMERAL_QUOTA_KEYS: tuple[str, ...] = (
    "requests.ephemeral-storage",
    "limits.ephemeral-storage",
)
# A pruned Namespace cascades through every object and PVC in it, which the db
# Kustomization's `deletionPolicy: Orphan` cannot stop; its caps stay with it.
PRUNE_ANNOTATION = "kustomize.toolkit.fluxcd.io/prune"
PRUNE_GUARDED_KINDS = ("Namespace", *REQUIRED_CAPS)
DATABASE_PATH = "kubernetes/db"
# Kinds with no namespace of their own, by API group: a CRD may reuse one of
# these names in another group, and that kind is namespaced like any other.
CLUSTER_SCOPED_KINDS = {
    "Namespace": "",
    "ClusterRole": "rbac.authorization.k8s.io",
    "ClusterRoleBinding": "rbac.authorization.k8s.io",
    "ClusterSecretStore": "external-secrets.io",
    "ClusterExternalSecret": "external-secrets.io",
    "ClusterIssuer": "cert-manager.io",
    "PriorityClass": "scheduling.k8s.io",
    "CustomResourceDefinition": "apiextensions.k8s.io",
    "ValidatingAdmissionPolicyBinding": "admissionregistration.k8s.io",
    "ValidatingAdmissionPolicy": "admissionregistration.k8s.io",
    "MutatingAdmissionPolicy": "admissionregistration.k8s.io",
    "MutatingAdmissionPolicyBinding": "admissionregistration.k8s.io",
    "ValidatingWebhookConfiguration": "admissionregistration.k8s.io",
    "MutatingWebhookConfiguration": "admissionregistration.k8s.io",
    "PersistentVolume": "",
    "StorageClass": "storage.k8s.io",
    "IngressClass": "networking.k8s.io",
    "RuntimeClass": "node.k8s.io",
    "APIService": "apiregistration.k8s.io",
}
# What a tenant file may declare outside its own namespaces, as (kind, API group);
# anything else there is platform scope. The grant ConfigMap and bindings are
# narrowed further by name and policy in _scope_problems.
TENANT_CLUSTER_KINDS = (("Namespace", ""), (STORE_KIND, "external-secrets.io"),
                        ("ValidatingAdmissionPolicyBinding", "admissionregistration.k8s.io"))
FLUX_SYSTEM_KINDS = (("Kustomization", FLUX_KUSTOMIZE_GROUP),
                     (SOURCE_KIND, "source.toolkit.fluxcd.io"), ("ServiceAccount", ""),
                     ("Role", RBAC_GROUP), ("RoleBinding", RBAC_GROUP), ("ConfigMap", ""))
# Fields the checks compare as names, per kind; a mapping or list there is a
# malformed manifest, reported by field before any check reads the file.
SCALAR_SPEC_FIELDS = {
    "Kustomization": ("serviceAccountName", "targetNamespace", "path",
                      "sourceRef.kind", "sourceRef.name", "sourceRef.namespace"),
    "ValidatingAdmissionPolicyBinding": ("policyName",),
}


def as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def storage_quota_keys(storage_classes: tuple[str, ...]) -> tuple[str, ...]:
    """The claim count always, plus the requests.storage of each size-blind class."""
    return (
        CLAIM_COUNT_KEY,
        *(f"{c}.storageclass.storage.k8s.io/requests.storage" for c in storage_classes),
    )


def tenant_files(tenants_dir: Path) -> list[Path]:
    return sorted(
        p for p in (*tenants_dir.glob("*.yaml"), *tenants_dir.glob("*.yml"))
        if p.name != AGGREGATOR and p.name not in SHARED
    )


def subjects(tenants_dir: Path, listed: set[str]) -> list[Path]:
    """Every tenant file Kustomize applies, plus every one on disk.

    Kustomize takes any suffix, so the subject is what the aggregator lists; an
    unlisted file on disk still trips "not listed"."""
    applied = {
        tenants_dir / r for r in (listed - SHARED - {AGGREGATOR})
        if (tenants_dir / r).is_file()
    }
    return sorted(applied | set(tenant_files(tenants_dir)))


def _secret_ref_namespaces(node: object, inside: bool = False) -> list[str | None]:
    """The `namespace` of each reference at or below a key ending in `secretRef`, any
    case, under the provider; None for a reference that names a Secret and no namespace."""
    found: list[str | None] = []
    if isinstance(node, dict):
        if inside and node.get("namespace") is not None:
            found.append(str(node["namespace"]))
        elif inside and "name" in node:
            found.append(None)
        for key, value in node.items():
            found += _secret_ref_namespaces(
                value, inside or str(key).lower().endswith("secretref")
            )
    elif isinstance(node, list):
        for item in node:
            found += _secret_ref_namespaces(item, inside)
    return found


def _provider_problems(store: dict, rel: str, name: str, namespaces: list[str]) -> list[str]:
    """A tenant store reads with a token from its own namespace and, for a vault
    provider, one vault named after a tenant namespace."""
    provider = as_mapping(as_mapping(store.get("spec")).get("provider"))
    kinds = sorted(str(k) for k in provider)
    if len(kinds) != 1 or kinds[0] not in STORE_PROVIDERS:
        shown = ", ".join(kinds) or "none"
        return [
            f"{rel}: {STORE_KIND} {name} uses provider {shown}, not exactly one of "
            f"{', '.join(STORE_PROVIDERS)} — its reach cannot be checked to stop at "
            "the tenant's own vault"
        ]
    kind = kinds[0]
    config = as_mapping(provider.get(kind))
    refs = _secret_ref_namespaces(config)
    problems = [
        f"{rel}: {STORE_KIND} {name} reads its credentials from namespace {ns}, not "
        "a tenant namespace — the tenant borrows another namespace's token"
        for ns in sorted({r for r in refs if r is not None} - set(namespaces))
    ]
    if None in refs:
        problems.append(
            f"{rel}: {STORE_KIND} {name} names no namespace on its credential secretRef "
            "— a cluster-scoped store must read its token from the tenant namespace "
            "explicitly"
        )
    if kind in TOKEN_PROVIDERS:
        return problems
    if kind == "onepasswordSDK":
        vault = config.get("vault")
        listed = [str(vault)] if isinstance(vault, str) and vault else []
    else:
        vaults = config.get("vaults")
        listed = sorted(str(v) for v in vaults) if isinstance(vaults, dict) else []
    if not listed:
        return problems + [
            f"{rel}: {STORE_KIND} {name} names no 1Password vault — the store "
            "cannot be checked to read only the tenant's own vault"
        ]
    if len(listed) == 1 and listed[0] in namespaces:
        return problems
    own = ", ".join(sorted(namespaces)) or "none declared"
    return problems + [
        f"{rel}: {STORE_KIND} {name} reads vault(s) {', '.join(listed)}, not the "
        f"tenant's own vault alone ({own}) — every item in another vault is "
        "readable from the tenant namespace"
    ]


def _store_problems(docs: list, rel: str, namespaces: list[str], labels: dict) -> list[str]:
    """A tenant-owned ClusterSecretStore is scoped to the tenant namespaces alone."""
    problems = []
    for store in of_kind(docs, STORE_KIND):
        name = metadata(store).get("name") or "?"
        problems += _provider_problems(store, rel, name, namespaces)
        raw = as_mapping(store.get("spec")).get("conditions")
        conditions = [c for c in as_list(raw) if isinstance(c, dict)]
        if not conditions:
            problems.append(
                f"{rel}: {STORE_KIND} {name} has no spec.conditions — a cluster-scoped "
                "store is referenceable from every namespace, so any ExternalSecret in "
                "the cluster can read its vault"
            )
            continue
        if len(conditions) != len(as_list(raw)):
            problems.append(
                f"{rel}: {STORE_KIND} {name} has a spec.conditions entry that is not "
                "a mapping, so its reach cannot be read"
            )
        if not namespaces:
            continue
        for ns in namespaces:
            if not any(condition_admits(c, ns, labels.get(ns, {})) for c in conditions):
                problems.append(
                    f"{rel}: {STORE_KIND} {name} conditions do not admit the tenant "
                    f"namespace {ns} — its ExternalSecrets never sync"
                )
        wider = sorted(
            {str(n) for c in conditions for n in as_list(c.get("namespaces"))}
            - set(namespaces)
        )
        loose = sorted(
            {
                key
                for c in conditions
                for key in ("namespaceRegexes", "namespaceSelector")
                if c.get(key) is not None
            }
        )
        if wider or loose:
            reach = ", ".join(wider + loose)
            problems.append(
                f"{rel}: {STORE_KIND} {name} reaches past "
                f"{_tenant_namespaces(namespaces)} ({reach}) — a tenant's store is "
                "scoped to its own namespaces alone"
            )
    return problems


def _caps_problems(docs: list, rel: str, claim_keys: tuple[str, ...]) -> list[str]:
    """Both caps are declared, the quota bounds claims and ephemeral storage, and
    every Container limit bounds ephemeral storage and sets no CPU under `default`
    or `max`: the apiserver copies a lone `max` into `default`, limiting every pod."""
    problems = []
    for kind in REQUIRED_CAPS:
        if not of_kind(docs, kind):
            problems.append(
                f"{rel}: declares no {kind} — the tenant's admin RoleBinding places "
                "no ceiling on what its pods may request"
            )
    quotas = of_kind(docs, "ResourceQuota")
    hard: set = set()
    for quota in quotas:
        hard |= set(as_mapping(as_mapping(quota.get("spec")).get("hard")))
    missing = [k for k in claim_keys if k not in hard]
    if quotas and missing:
        problems.append(
            f"{rel}: ResourceQuota does not cap persistent volume claims; missing "
            f"spec.hard keys {', '.join(missing)} — nothing else bounds how many "
            "claims the tenant declares, or their size on a class that enforces none"
        )
    missing = [k for k in EPHEMERAL_QUOTA_KEYS if k not in hard]
    if quotas and missing:
        problems.append(
            f"{rel}: ResourceQuota does not cap ephemeral storage; missing "
            f"spec.hard keys {', '.join(missing)} — emptyDir and writable layers "
            "would be bounded only by the node's disk"
        )
    for limit_range in of_kind(docs, "LimitRange"):
        name = metadata(limit_range).get("name") or "?"
        entries = [
            e for e in as_list(as_mapping(limit_range.get("spec")).get("limits"))
            if isinstance(e, dict) and e.get("type") == "Container"
        ]
        if not entries:
            problems.append(
                f"{rel}: LimitRange {name} has no Container entry — the defaults "
                "and ceilings never apply, so a container with no resources block "
                "lands unbounded inside the quota"
            )
        for entry in entries:
            cpu_keys = [k for k in ("default", "max") if "cpu" in as_mapping(entry.get(k))]
            if cpu_keys:
                problems.append(
                    f"{rel}: LimitRange {name} sets cpu under "
                    f"{', '.join(cpu_keys)} — every tenant container would be "
                    "admitted with a CPU limit and CFS-throttled"
                )
            # Without defaultRequest a container with no resources block is
            # charged the whole limit against the quota and may be refused.
            unset = [
                k for k in ("default", "defaultRequest", "max")
                if "ephemeral-storage" not in as_mapping(entry.get(k))
            ]
            if unset:
                problems.append(
                    f"{rel}: LimitRange {name} sets no ephemeral-storage under "
                    f"{', '.join(unset)} — a tenant container with no resources block "
                    "would spend the node's disk unbounded, or be refused by the quota"
                )
    return problems


def _is_database_tree(kustomization: dict) -> bool:
    """A `-db` Kustomization, or one reconciling the database tree or under it."""
    name = str(metadata(kustomization).get("name") or "")
    path = posixpath.normpath(str(as_mapping(kustomization.get("spec")).get("path") or "."))
    tree = path.lstrip("/")
    return (
        name.endswith("-db")
        or tree == DATABASE_PATH
        or tree.startswith(DATABASE_PATH + "/")
    )


def _flux_kustomizations(docs: list) -> list[dict]:
    return [k for k in of_kind(docs, "Kustomization") if api_group(k) == FLUX_KUSTOMIZE_GROUP]


def _guarded(doc: dict) -> bool:
    return as_mapping(metadata(doc).get("annotations")).get(PRUNE_ANNOTATION) == "disabled"


def _prune_guard_problems(docs: list, rel: str) -> list[str]:
    """Every Namespace, its caps, hostname grant and grant bindings outlive the wiring."""
    database = any(_is_database_tree(k) for k in _flux_kustomizations(docs))
    problems = []
    for kind in PRUNE_GUARDED_KINDS:
        for doc in of_kind(docs, kind):
            if _guarded(doc):
                continue
            if kind != "Namespace":
                why = ("removing the wiring would prune it and leave the surviving "
                       "Namespace without that ceiling")
            elif database:
                why = ("removing the wiring would prune the Namespace and "
                       "cascade-delete the database and its PVCs, whatever the db "
                       "Kustomization's deletionPolicy says")
            else:
                why = ("removing the wiring would prune the Namespace and "
                       "cascade-delete every object and PVC in it")
            problems.append(
                f"{rel}: {kind} {metadata(doc).get('name')} lacks "
                f"{PRUNE_ANNOTATION}: disabled — {why}"
            )
    admission = [
        d for d in of_kind(docs, "ConfigMap")
        if str(metadata(d).get("name") or "").startswith(GRANT_PREFIX)
    ] + [
        d for d in of_kind(docs, "ValidatingAdmissionPolicyBinding")
        if as_mapping(d.get("spec")).get("policyName") in GRANT_POLICIES
    ]
    for doc in admission:
        if not _guarded(doc):
            problems.append(
                f"{rel}: {doc.get('kind')} {metadata(doc).get('name')} lacks "
                f"{PRUNE_ANNOTATION}: disabled — removing the wiring would prune it "
                "while the Namespace survives, and admission for the Namespace must "
                "outlive the wiring until the Namespace is deleted by hand"
            )
    return problems


def _cluster_scoped(doc: dict) -> bool:
    kind = str(doc.get("kind") or "")
    return kind in CLUSTER_SCOPED_KINDS and api_group(doc) == CLUSTER_SCOPED_KINDS[kind]


def _tenant_namespaces(namespaces: list[str]) -> str:
    if len(namespaces) == 1:
        return f"the tenant namespace {namespaces[0]}"
    return f"a tenant namespace ({', '.join(namespaces)})"


def _placement_problems(docs: list, rel: str, namespaces: list[str]) -> list[str]:
    """Every namespaced document sits in a tenant namespace or flux-system.

    The tenants aggregator sets no targetNamespace, so a document that names no
    namespace lands in `default`, outside the tenant."""
    problems = []
    allowed = {*namespaces, FLUX_NAMESPACE}
    for doc in docs:
        if _cluster_scoped(doc):
            continue
        kind = str(doc.get("kind") or "?")
        name = metadata(doc).get("name") or "?"
        where = metadata(doc).get("namespace")
        if not where:
            problems.append(
                f"{rel}: {kind} {name} names no metadata.namespace, so it lands in "
                "`default` rather than the tenant namespace"
            )
        elif namespaces and where not in allowed:
            problems.append(
                f"{rel}: {kind} {name} sits in namespace {where}, which is neither "
                f"{_tenant_namespaces(namespaces)} nor {FLUX_NAMESPACE}"
            )
    return problems


def _scope_problems(docs: list, rel: str, namespaces: list[str]) -> list[str]:
    """Outside its own namespaces a tenant file declares only its Namespaces, store and
    grant bindings, and in flux-system only its Flux objects, their RBAC and its grant."""
    problems = []
    for doc in docs:
        kind, group = str(doc.get("kind") or "?"), api_group(doc)
        name = metadata(doc).get("name") or "?"
        if _cluster_scoped(doc):
            allowed = (kind, group) in TENANT_CLUSTER_KINDS
            policy = as_mapping(doc.get("spec")).get("policyName")
            if allowed and kind == "ValidatingAdmissionPolicyBinding" and policy in GRANT_POLICIES:
                # Named and shaped for one of this file's namespaces, so it reaches no other.
                if not any(name == f"{policy}-{ns}" and doc.get("spec") == routing_binding_spec(ns, policy)
                           for ns in namespaces):
                    problems.append(
                        f"{rel}: ValidatingAdmissionPolicyBinding {name} of {policy} is not "
                        f"{policy}-<namespace> with the Deny spec on one tenant namespace "
                        f"of this file — it could bind {policy} beyond the tenant"
                    )
                continue
            if not allowed or kind == "ValidatingAdmissionPolicyBinding":
                problems.append(
                    f"{rel}: cluster-scoped {kind} {name} is platform scope — a tenant "
                    f"file declares only Namespace, {STORE_KIND} and bindings of "
                    f"{', '.join(GRANT_POLICIES)} outside its namespaces"
                )
        elif metadata(doc).get("namespace") == FLUX_NAMESPACE:
            allowed = (kind, group) in FLUX_SYSTEM_KINDS and (
                kind != "ConfigMap" or name in {GRANT_PREFIX + ns for ns in namespaces}
            )
            if not allowed:
                problems.append(
                    f"{rel}: {kind} {name} in {FLUX_NAMESPACE} is platform scope — a "
                    "tenant file places there only its Kustomizations, GitRepositories, "
                    "ServiceAccounts, Role, RoleBinding and its "
                    f"{GRANT_PREFIX}<namespace> ConfigMap"
                )
    return problems


def _strings(value: object) -> list[str] | None:
    """A non-empty list of non-empty strings, else None."""
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return value
    return None


def _rule_problem(rule: object, declared: dict[str, set[str]]) -> str | None:
    """Why a flux-system Role rule grants more than reading this file's own Flux objects."""
    rule = as_mapping(rule)
    groups = rule.get("apiGroups")
    resources = _strings(rule.get("resources"))
    names, verbs = _strings(rule.get("resourceNames")), _strings(rule.get("verbs"))
    # The core group is the empty string, so the one apiGroup may be "".
    if set(rule) != {"apiGroups", "resources", "resourceNames", "verbs"} or not (
        isinstance(groups, list) and len(groups) == 1 and isinstance(groups[0], str)
        and resources and names and verbs
    ):
        return ("is not one apiGroup with non-empty string lists of resources, "
                "resourceNames and verbs")
    group = groups[0]
    if group not in READABLE_FLUX_OBJECTS or set(resources) != {READABLE_FLUX_OBJECTS[group][0]}:
        return f"reaches {group or 'core'} {', '.join(resources)}, not this tenant's own Flux objects"
    if not set(verbs) <= READ_VERBS:
        return f"grants {', '.join(sorted(set(verbs) - READ_VERBS))}, not read alone"
    foreign = sorted(set(names) - declared[group])
    if foreign:
        return f"names {', '.join(foreign)}, which this file does not declare"
    return None


def _flux_system_rbac_problems(docs: list, rel: str, namespaces: list[str]) -> list[str]:
    """In flux-system a tenant reads its own Flux objects and nothing else: each Role
    reads this file's Kustomizations or GitRepositories, and each RoleBinding binds
    such a Role to a ServiceAccount this file declares."""
    rbac = [d for d in docs if api_group(d) == RBAC_GROUP]
    problems = []
    declared = {
        group: {
            str(metadata(d).get("name"))
            for d in of_kind(docs, kind)
            if api_group(d) == group and metadata(d).get("namespace") == FLUX_NAMESPACE
        }
        for group, (_, kind) in READABLE_FLUX_OBJECTS.items()
    }
    # An unsafe Role is reported once, on itself; its binding still needs this file's Role.
    roles = set()
    for role in of_kind(rbac, "Role"):
        if metadata(role).get("namespace") != FLUX_NAMESPACE:
            continue
        name = metadata(role).get("name") or "?"
        roles.add(name)
        rules = role.get("rules")
        why = (
            [w for r in rules if (w := _rule_problem(r, declared))]
            if isinstance(rules, list) and rules else ["has no rules list"]
        )
        problems += [
            f"{rel}: Role {FLUX_NAMESPACE}/{name} {w} — a tenant may only read its "
            f"own Flux objects in {FLUX_NAMESPACE}"
            for w in why
        ]
    accounts = {
        (str(metadata(d).get("name")), str(metadata(d).get("namespace")))
        for d in of_kind(docs, "ServiceAccount")
        if metadata(d).get("namespace") in {*namespaces, FLUX_NAMESPACE}
    }
    for binding in of_kind(rbac, "RoleBinding"):
        if metadata(binding).get("namespace") != FLUX_NAMESPACE:
            continue
        name = metadata(binding).get("name") or "?"
        ref = as_mapping(binding.get("roleRef"))
        expected = {"apiGroup": RBAC_GROUP, "kind": "Role", "name": ref.get("name")}
        if ref != expected or ref.get("name") not in roles:
            problems.append(
                f"{rel}: RoleBinding {FLUX_NAMESPACE}/{name} binds {ref.get('kind') or '?'} "
                f"{ref.get('name') or '?'}, not a Role this file declares in "
                f"{FLUX_NAMESPACE} — it would grant the tenant more of {FLUX_NAMESPACE}"
            )
        subjects = as_list(binding.get("subjects"))
        strangers = [
            s for s in subjects
            if as_mapping(s).get("kind") != "ServiceAccount"
            or (str(as_mapping(s).get("name")), str(as_mapping(s).get("namespace")))
            not in accounts
        ]
        if strangers or not subjects:
            problems.append(
                f"{rel}: RoleBinding {FLUX_NAMESPACE}/{name} binds a subject other than a "
                "ServiceAccount this file declares — the grant would reach past the tenant"
            )
    return problems


def platform_namespaces(root: Path, tenants_dir: Path) -> tuple[dict[str, str], list[str]]:
    """{name: file} for each Namespace the cluster tree declares outside the tenants
    directory, plus the built-ins, and a finding per file that does not parse.
    OperatorError when the tree is absent or a file is unreadable."""
    found = {name: "the apiserver" for name in BUILTIN_NAMESPACES}
    problems = []
    tree = root / PLATFORM_TREE
    if not tree.is_dir():
        raise OperatorError(
            f"{tree} is not a directory, so no platform Namespace is inspected for "
            "collisions — pass the cluster repo root as --repo-root"
        )
    files = (p for p in sorted(tree.rglob("*")) if p.suffix in (".yaml", ".yml") and p.is_file())
    for path in files:
        if path.resolve().is_relative_to(tenants_dir.resolve()):
            continue
        where = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise OperatorError(f"{where}: unreadable: {exc}") from exc
        try:
            docs = load_docs(text)
        except OperatorError:
            problems.append(
                f"{where}: does not parse, so a tenant could claim a namespace it declares"
            )
            continue
        for doc in of_kind(docs, "Namespace"):
            name = metadata(doc).get("name")
            if isinstance(name, str) and api_group(doc) == "":
                found.setdefault(name, where)
    return found, problems


def _collision_problems(namespaces: list[str], rel: str, platform: dict[str, str]) -> list[str]:
    """A tenant Namespace the platform already declares hands the tenant that namespace."""
    return [
        f"{rel}: namespace {ns} is already declared by {platform[ns]} — the tenant "
        "would take over a platform namespace and everything running in it"
        for ns in namespaces if ns in platform
    ]


def _duplicate_problems(paths: list[Path], targets: Targets) -> list[str]:
    """A namespace declared by two tenant files binds admin there to both tenants."""
    owner: dict[str, str] = {}
    problems = []
    for path in paths:
        rel = targets.label(path)
        try:
            docs = load_mappings(path, rel)
        except OperatorError:
            continue  # check_file reports the parse failure
        for doc in of_kind(docs, "Namespace"):
            ns = metadata(doc).get("name")
            if not ns:
                continue
            first = owner.setdefault(str(ns), rel)
            if first != rel:
                problems.append(
                    f"{rel}: namespace {ns} is also declared by {first} — both "
                    "tenants' service accounts would hold admin in it"
                )
    return problems


def check_file(path: Path, rel: str, listed: set[str] | None,
               root: Path | None = None,
               storage_classes: tuple[str, ...] = (),
               tenants_dir: Path | None = None,
               platform: dict[str, str] | None = None) -> list[str]:
    """One wiring file's findings; `listed` None checks it with no aggregator."""
    # A wiring file that does not parse is a finding here, not an empty inspection.
    try:
        docs = load_mappings(path, rel)
    except OperatorError as exc:
        return [str(exc)]

    problems = []
    name = path.relative_to(tenants_dir).as_posix() if tenants_dir else path.name
    if listed is not None and name not in listed:
        problems.append(
            f"{rel}: not listed in {AGGREGATOR} resources — Kustomize does not "
            "auto-discover, so this tenant is never applied"
        )

    shape = _shape_problems(docs, rel)
    if shape:
        return problems + shape

    declared = of_kind(docs, "Namespace")
    namespaces = [str(metadata(d).get("name")) for d in declared if metadata(d).get("name")]
    # The apiserver stamps the name label on every Namespace, so a selector may use it.
    labels = {
        str(metadata(d).get("name")): {
            **as_mapping(metadata(d).get("labels")), NAME_LABEL: str(metadata(d).get("name")),
        }
        for d in declared
    }
    if not declared:
        problems.append(f"{rel}: declares no Namespace")
    # Each Namespace in the file is admitted on its own labels, so each carries them.
    for doc in declared:
        present = as_mapping(metadata(doc).get("labels"))
        for label in REQUIRED_NS_LABELS:
            if label not in present:
                problems.append(
                    f"{rel}: namespace {metadata(doc).get('name')} has no {label} label"
                )
        for label, level in PSS_LEVELS.items():
            if label in present and present[label] != level:
                problems.append(
                    f"{rel}: namespace {metadata(doc).get('name')} sets {label}: "
                    f"{present[label]}, not {level} — the tenant contract pins Pod "
                    "Security at enforce baseline, warn and audit restricted"
                )
        # A pinned version runs an older, weaker profile than the level names.
        for label in sorted(present):
            if (str(label).startswith(PSS_PREFIX) and str(label).endswith("-version")
                    and present[label] != "latest"):
                problems.append(
                    f"{rel}: namespace {metadata(doc).get('name')} sets {label}: "
                    f"{present[label]}, not latest — an older profile admits pods the "
                    "current one refuses"
                )

    problems += _collision_problems(namespaces, rel, platform or {})
    problems += _caps_problems(docs, rel, storage_quota_keys(storage_classes))
    problems += _prune_guard_problems(docs, rel)
    problems += _placement_problems(docs, rel, namespaces)
    problems += _scope_problems(docs, rel, namespaces)
    problems += _flux_system_rbac_problems(docs, rel, namespaces)
    problems += _store_problems(docs, rel, namespaces, labels)

    kustomizations = _flux_kustomizations(docs)
    if not kustomizations:
        problems.append(f"{rel}: declares no Flux Kustomization")
        return problems
    for kustomization in kustomizations:
        label = f"{rel} [{metadata(kustomization).get('name', '?')}]"
        problems += _kustomization_problems(docs, label, kustomization, namespaces, root)
    return problems


def _shape_problems(docs: list, rel: str) -> list[str]:
    """Every field the checks compare as a name is a scalar, not a mapping or list."""
    problems = []
    for doc in docs:
        kind = str(doc.get("kind") or "?")
        meta = metadata(doc)
        fields = [(f"metadata.{f}", meta.get(f)) for f in ("name", "namespace")]
        spec = as_mapping(doc.get("spec"))
        for path in SCALAR_SPEC_FIELDS.get(kind, ()):
            head, _, tail = path.partition(".")
            value = as_mapping(spec.get(head)).get(tail) if tail else spec.get(head)
            fields.append((f"spec.{path}", value))
        name = meta.get("name") if isinstance(meta.get("name"), str) else "?"
        problems += [
            f"{rel}: malformed manifest shape — {kind} {name} {where} is a "
            f"{type(value).__name__}, not a scalar"
            for where, value in fields if isinstance(value, (dict, list))
        ]
    return problems


def _kustomization_problems(docs: list, label: str, kustomization: dict,
                            namespaces: list[str], root: Path | None) -> list[str]:
    """One Flux Kustomization's confinement; every one in the file is checked."""
    problems = []
    spec = as_mapping(kustomization.get("spec"))
    service_account = spec.get("serviceAccountName")
    if not service_account:
        problems.append(
            f"{label}: Kustomization has no serviceAccountName — it would reconcile "
            "as kustomize-controller's own cluster-admin"
        )
    if spec.get("prune") is not True:
        problems.append(f"{label}: Kustomization does not set prune: true")
    if _is_database_tree(kustomization) and spec.get("deletionPolicy") != "Orphan":
        problems.append(
            f"{label}: database Kustomization does not set deletionPolicy: Orphan — "
            "deleting it would garbage-collect the database Cluster and its PVCs"
        )
    target = spec.get("targetNamespace")
    if not target:
        problems.append(f"{label}: Kustomization has no targetNamespace")
    elif namespaces and target not in namespaces:
        problems.append(
            f"{label}: Kustomization targetNamespace {target!r} is not the tenant "
            f"namespace {namespaces[0]!r}"
            + (f" or another the file declares ({', '.join(namespaces[1:])})"
               if len(namespaces) > 1 else "")
        )
    problems += _substitute_problems(spec, label)
    problems += _source_problems(docs, label, spec)
    problems += _path_problems(label, spec, root)
    depends = {
        str(as_mapping(d).get("name")) for d in as_list(spec.get("dependsOn"))
    }
    if REQUIRED_DEPENDENCY not in depends:
        problems.append(
            f"{label}: Kustomization does not dependOn {REQUIRED_DEPENDENCY} — on a "
            "fresh bootstrap it applies before the platform CRDs exist"
        )

    if service_account:
        accounts = {
            (metadata(d).get("name"), metadata(d).get("namespace"))
            for d in of_kind(docs, "ServiceAccount")
        }
        if (service_account, FLUX_NAMESPACE) not in accounts:
            problems.append(
                f"{label}: no ServiceAccount {service_account} in {FLUX_NAMESPACE} — "
                "kustomize-controller impersonates it from its own namespace"
            )
        home = target if target in namespaces else (namespaces[0] if namespaces else None)
        problems += _rolebinding_problems(docs, label, service_account, home)
    return problems


def _substitute_problems(spec: dict, rel: str) -> list[str]:
    """Flux substitutes nothing a Kustomization does not ask for, so a tenant
    without cluster-config applies its ${...} placeholders literally. Every
    entry is one of the platform ConfigMaps, named once."""
    problems = []
    entries: dict = {}
    post_build = spec.get("postBuild") or {}
    if not isinstance(post_build, dict):
        problems.append(f"{rel}: postBuild is not a mapping, so nothing is substituted")
        post_build = {}
    sources = post_build.get("substituteFrom") or []
    if not isinstance(sources, list):
        problems.append(f"{rel}: substituteFrom is not a list, so nothing is substituted")
        sources = []
    for entry in sources:
        if not isinstance(entry, dict):
            problems.append(f"{rel}: substituteFrom entry {entry!r} is not a mapping")
            continue
        kind, name = entry.get("kind"), str(entry.get("name") or "")
        if kind != "ConfigMap":
            problems.append(
                f"{rel}: substituteFrom {kind or 'entry with no kind'} {name or '?'} "
                "is not a ConfigMap — a tenant substitutes from the platform "
                "ConfigMaps only, never a Secret"
            )
            continue
        if name not in ALLOWED_SUBSTITUTE_SOURCES:
            problems.append(
                f"{rel}: substituteFrom ConfigMap {name or '?'} is not one of "
                f"{', '.join(ALLOWED_SUBSTITUTE_SOURCES)} — the platform publishes "
                "no other substitution source to tenants"
            )
            continue
        if name in entries:
            problems.append(
                f"{rel}: substituteFrom lists ConfigMap {name} more than once — "
                "each platform source belongs in the list once"
            )
            continue
        entries[name] = entry
    for name in REQUIRED_SUBSTITUTE_SOURCES:
        if name not in entries:
            problems.append(
                f"{rel}: Kustomization does not substituteFrom ConfigMap {name} — "
                "its ${...} placeholders would be applied literally"
            )
    for name in sorted(entries):
        if entries[name].get("optional") is not False:
            problems.append(
                f"{rel}: substituteFrom ConfigMap {name} is not optional: false — "
                "a missing ConfigMap would apply ${...} placeholders literally"
            )
    return problems


def _source_problems(docs: list, rel: str, spec: dict) -> list[str]:
    """The Kustomization reconciles the tenant's own repository, declared here."""
    source = as_mapping(spec.get("sourceRef"))
    name = source.get("name")
    if not name:
        return [
            f"{rel}: Kustomization has no sourceRef.name — it reconciles no "
            "tenant repository"
        ]
    if source.get("kind") != SOURCE_KIND:
        return [
            f"{rel}: Kustomization sourceRef.kind is {source.get('kind')!r}, not "
            f"{SOURCE_KIND}"
        ]
    namespace = source.get("namespace") or FLUX_NAMESPACE
    if namespace != FLUX_NAMESPACE:
        return [
            f"{rel}: Kustomization sourceRef.namespace is {namespace!r}; the "
            f"tenant source lives in {FLUX_NAMESPACE} beside the Kustomization"
        ]
    sources = {
        (metadata(d).get("name"), metadata(d).get("namespace"))
        for d in of_kind(docs, SOURCE_KIND)
    }
    if (name, FLUX_NAMESPACE) not in sources:
        return [
            f"{rel}: no {SOURCE_KIND} {name} in {FLUX_NAMESPACE} — the tenant's "
            "own source is declared in its wiring file, so this reference "
            "either points at another tenant's repo or at nothing"
        ]
    return []


def _path_problems(rel: str, spec: dict, root: Path | None) -> list[str]:
    """`spec.path` stays inside the tenant repository. It resolves in the
    tenant's checkout, so a directory of this repo by that name means it was
    aimed at the cluster's own tree; with no cluster root that arm is skipped."""
    raw = spec.get("path")
    if not raw:
        return [
            f"{rel}: Kustomization has no spec.path — it reconciles the tenant "
            "repository root, every manifest in it included"
        ]
    path = posixpath.normpath(str(raw).lstrip("/"))
    if path == ".." or path.startswith("../"):
        return [
            f"{rel}: Kustomization spec.path {raw!r} climbs above the tenant "
            "repository root"
        ]
    if root is not None and (root / path).is_dir():
        return [
            f"{rel}: Kustomization spec.path {raw!r} is a directory of this "
            "cluster repo — the tenant ServiceAccount would reconcile the "
            "cluster's own manifests into the tenant namespace"
        ]
    return []


def _rolebinding_problems(docs: list, rel: str, account: str,
                          namespace: str | None) -> list[str]:
    problems = []
    for role in REQUIRED_ROLES:
        named = [
            b
            for b in of_kind(docs, "RoleBinding")
            if as_mapping(b.get("roleRef")).get("name") == role
            and any(
                as_mapping(s).get("kind") == "ServiceAccount"
                and as_mapping(s).get("name") == account
                and as_mapping(s).get("namespace") == FLUX_NAMESPACE
                for s in as_list(b.get("subjects"))
            )
        ]
        expected = {"apiGroup": RBAC_GROUP, "kind": "ClusterRole", "name": role}
        bound = [b for b in named if api_group(b) == RBAC_GROUP and b.get("roleRef") == expected]
        if not bound:
            problems.append(
                f"{rel}: no RoleBinding grants {role} to {FLUX_NAMESPACE}/{account}"
                if not named else
                f"{rel}: the {role} RoleBinding for {FLUX_NAMESPACE}/{account} does not "
                f"reference ClusterRole {role} in {RBAC_GROUP} — a tenant-local Role "
                "of that name grants whatever the tenant writes into it"
            )
            continue
        if namespace and all(metadata(b).get("namespace") != namespace for b in bound):
            problems.append(
                f"{rel}: the {role} RoleBinding is not in the tenant namespace {namespace}"
            )
    return problems


def check_directory(targets: Targets, storage_classes: tuple[str, ...] = (),
                    listed: set[str] | None = None,
                    paths: list[Path] | None = None) -> list[str]:
    """`listed` and `paths` take an aggregator listing and subjects already read."""
    tenants_dir = targets.tenants_dir
    if listed is None:
        listed = listed_resources(tenants_dir)
    platform, problems = platform_namespaces(targets.root, tenants_dir)
    if paths is None:
        paths = subjects(tenants_dir, listed)
    for path in paths:
        problems += check_file(
            path, targets.label(path), listed, root=targets.root,
            storage_classes=storage_classes, tenants_dir=tenants_dir, platform=platform,
        )
    problems += _duplicate_problems(paths, targets)
    where = targets.label(tenants_dir / AGGREGATOR)
    for resource in sorted(listed - SHARED):
        target = tenants_dir / resource
        if not target.exists():
            problems.append(f"{where} lists {resource}, which does not exist")
        elif not target.is_file():
            problems.append(
                f"{where} lists {resource}, which is not a regular file, so no tenant "
                "wiring in it is checked"
            )
    return problems


def check_files(targets: Targets, storage_classes: tuple[str, ...] = ()) -> list[str]:
    """Standalone wiring files: no aggregator listing, no cluster-tree path arm."""
    builtins = {name: "the apiserver" for name in BUILTIN_NAMESPACES}
    problems = []
    for path in targets.files:
        problems += check_file(
            path, targets.label(path), None, root=None, storage_classes=storage_classes,
            platform=builtins,
        )
    return problems + _duplicate_problems(targets.files, targets)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Tenant wiring completeness")
    add_target_arguments(parser, Path(__file__).resolve().parent.parent)
    # One of the two is required, so an omitted flag never drops the size caps.
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument(
        "--tenant-storage-class", action="store_true",
        help=f"{TENANT_STORAGE_CLASS} enforces no claim size, so the quota must "
             "cap its requests.storage",
    )
    storage.add_argument(
        "--no-tenant-storage-class", action="store_true",
        help=f"{TENANT_STORAGE_CLASS} enforces a claim size itself",
    )
    args = parser.parse_args(argv)
    classes = (TENANT_STORAGE_CLASS,) if args.tenant_storage_class else ()

    try:
        targets = resolve_targets(args)
        if targets.files:
            problems = check_files(targets, classes)
            count = len(targets.files)
        else:
            tenants_dir = targets.tenants_dir
            listed = listed_resources(tenants_dir)
            where = targets.label(tenants_dir)
            paths = subjects(tenants_dir, listed)
            count = len(paths)
            if not count:
                # An empty tree is a fact only when the aggregator agrees with it.
                unseen = sorted(listed - SHARED - {AGGREGATOR})
                if unseen:
                    raise OperatorError(
                        f"{where}/{AGGREGATOR} lists {', '.join(unseen)}, which "
                        "the tenant-file scan did not reach"
                    )
                print("check-tenant-wiring: no tenant onboarded, and none listed.")
                return 0
            problems = check_directory(targets, classes, listed, paths)
    except (OperatorError, OSError, UnicodeDecodeError) as exc:
        print(f"check-tenant-wiring inspected nothing: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("Tenant wiring is incomplete:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    scope = "given" if targets.files else f"in {where}"
    print(f"Tenant wiring OK ({count} tenant file(s) {scope}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
