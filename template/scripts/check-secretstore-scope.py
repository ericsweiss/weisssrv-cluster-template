#!/usr/bin/env python3
"""Assert every ClusterSecretStore is namespace-scoped and covers its consumers.

A ClusterSecretStore with no `spec.conditions` is referenceable from EVERY
namespace: any principal that can create an ExternalSecret anywhere can mint any
item in the backing vault. Both directions are checked — every store declares
conditions, and every ExternalSecret (plus every namespace a
ClusterExternalSecret fans out to) sits in a namespace those conditions admit.
Matching mirrors ESO: any condition matching admits, via exact `namespaces`,
`namespaceRegexes`, or `namespaceSelector`; a fan-out is the UNION of
`namespaceSelectors` and literal `namespaces`.

A store REFERENCED but not defined in the corpus is a violation (the
ExternalSecret never syncs and its Secret goes stale); one that genuinely lives
outside the linted tree is declared with `--external-store NAME`. Exit 0 clean,
1 on a finding, 2 on an operator error including a vacuous corpus.

Usage (wired into flux:lint, on the accumulated full corpus):
  kustomize build <path> | envsubst >> corpus
  python3 scripts/check-secretstore-scope.py [--external-store NAME] < corpus
"""
from __future__ import annotations

import argparse
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

CLUSTER_STORE_KIND = "ClusterSecretStore"


def _selector_matches(selector: dict, labels: dict) -> bool:
    """Kubernetes labelSelector semantics (matchLabels + matchExpressions, ANDed)."""
    if selector is None:
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key = expr.get("key")
        op = expr.get("operator")
        values = expr.get("values") or []
        present = key in labels
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and not present:
            return False
        if op == "DoesNotExist" and present:
            return False
    return True


def _condition_admits(condition: dict, namespace: str, labels: dict) -> bool:
    if namespace in (condition.get("namespaces") or []):
        return True
    for pattern in condition.get("namespaceRegexes") or []:
        if re.search(pattern, namespace):
            return True
    selector = condition.get("namespaceSelector")
    if selector is not None and _selector_matches(selector, labels):
        return True
    return False


class CorpusError(RuntimeError):
    """Unparseable input — an operator error (exit 2), not a violation (exit 1)."""


def _load(stream) -> list[dict]:
    docs: list[dict] = []
    try:
        for raw in yaml.safe_load_all(stream):
            if isinstance(raw, dict):
                if raw.get("kind") == "List" and isinstance(raw.get("items"), list):
                    docs.extend(i for i in raw["items"] if isinstance(i, dict))
                else:
                    docs.append(raw)
            elif isinstance(raw, list):
                docs.extend(i for i in raw if isinstance(i, dict))
    except yaml.YAMLError as exc:
        raise CorpusError(f"Failed to parse YAML input: {exc}") from exc
    return docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Every ClusterSecretStore is namespace-scoped and covers its consumers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--external-store",
        action="append",
        default=[],
        metavar="NAME",
        help="ClusterSecretStore defined outside this corpus; referencing it is not a violation",
    )
    args = parser.parse_args(argv)
    external = set(args.external_store)

    try:
        docs = _load(sys.stdin)
    except CorpusError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not docs:
        print(
            "ERROR: empty corpus — no manifests on stdin. A gate that passes on nothing "
            "is not a gate; check the pipe and the `kustomize build` paths feeding it.",
            file=sys.stderr,
        )
        return 2

    ns_labels: dict[str, dict] = {}
    stores: dict[str, list] = {}
    # (store, namespace, describing the consumer) tuples to validate.
    consumers: list[tuple[str, str, str]] = []
    cluster_external_secrets: list[dict] = []

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        name = meta.get("name", "?")
        spec = doc.get("spec") or {}

        if kind == "Namespace":
            ns_labels[name] = meta.get("labels") or {}
        elif kind == CLUSTER_STORE_KIND:
            stores[name] = spec.get("conditions")
        elif kind == "ExternalSecret":
            ref = spec.get("secretStoreRef") or {}
            if ref.get("kind") == CLUSTER_STORE_KIND:
                ns = meta.get("namespace") or "default"
                consumers.append((ref.get("name", "?"), ns, f"ExternalSecret {ns}/{name}"))
        elif kind == "ClusterExternalSecret":
            cluster_external_secrets.append(doc)

    violations: list[str] = []

    for name, conditions in sorted(stores.items()):
        if not conditions:
            violations.append(
                f"  ClusterSecretStore {name}: no spec.conditions — referenceable "
                f"from every namespace, so any ExternalSecret in the cluster can "
                f"read the whole backing vault. Add conditions scoping it to the "
                f"namespaces that legitimately consume it."
            )

    # A ClusterExternalSecret creates ExternalSecrets in every namespace its
    # selectors match, so those namespaces need the same admission.
    for ces in cluster_external_secrets:
        meta = ces.get("metadata") or {}
        spec = ces.get("spec") or {}
        ref = ((spec.get("externalSecretSpec") or {}).get("secretStoreRef")) or {}
        if ref.get("kind") != CLUSTER_STORE_KIND:
            continue
        selectors = spec.get("namespaceSelectors")
        if selectors is None:
            # ABSENT vs EMPTY: `namespaceSelector: {}` is a label selector with
            # no terms, which matches EVERY namespace — the widest possible
            # fan-out. Truthiness would collapse it to "selects nothing" and skip
            # the one shape that most needs checking.
            single = spec.get("namespaceSelector")
            selectors = [] if single is None else [single]
        targets = {
            ns for ns, labels in ns_labels.items()
            if any(_selector_matches(sel, labels) for sel in selectors)
        }
        # ESO UNIONS the selectors with the literal `spec.namespaces` list, and a
        # CES written with the list alone matched no selector — so it used to
        # contribute zero consumers and its fan-out was never checked at all.
        targets.update(str(ns) for ns in spec.get("namespaces") or [])
        for ns in sorted(targets):
            consumers.append(
                (
                    ref.get("name", "?"),
                    ns,
                    f"ClusterExternalSecret {meta.get('name', '?')} -> {ns}",
                )
            )

    unknown: set[str] = set()
    for store, namespace, description in consumers:
        if store not in stores:
            if store not in external:
                unknown.add(store)
            continue
        conditions = stores[store] or []
        if not conditions:
            continue  # already reported as unscoped above
        labels = ns_labels.get(namespace, {})
        if not any(_condition_admits(c, namespace, labels) for c in conditions):
            violations.append(
                f"  {description}: namespace {namespace!r} is not admitted by "
                f"ClusterSecretStore {store}'s spec.conditions — ESO will refuse "
                f"the fetch and the Secret will go stale. Add the namespace to the "
                f"store's conditions (or point the app at a scoped store)."
            )

    for store in sorted(unknown):
        violations.append(
            f"  ClusterSecretStore {store}: referenced but not defined in this corpus, "
            f"so its scope cannot be checked — and at runtime an ExternalSecret pointing "
            f"at a store that does not resolve never syncs, leaving a stale Secret. Add "
            f"the store's manifest to the linted tree, or declare it with "
            f"--external-store {store} if it is genuinely managed elsewhere."
        )

    if violations:
        print(
            "ClusterSecretStore scoping invariant violated:", file=sys.stderr
        )
        print("\n".join(sorted(set(violations))), file=sys.stderr)
        return 1

    if not stores and not consumers:
        # A non-empty corpus that holds neither is the likelier wiring failure:
        # the render loop produced output but never reached the stage defining
        # the stores, or the accumulator was stale/truncated.
        print(
            f"ERROR: inspected 0 ClusterSecretStores and 0 namespace consumers in "
            f"{len(docs)} document(s) — a gate that checks nothing is not a gate. "
            "Check that the `kustomize build` paths feeding stdin cover the stage "
            "that defines the stores.",
            file=sys.stderr,
        )
        return 2

    external_note = f", {len(external)} declared external" if external else ""
    print(
        f"ClusterSecretStore scoping OK ({len(stores)} cluster stores, "
        f"{len(consumers)} namespace consumers checked{external_note})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
