#!/usr/bin/env python3
"""Shared loader, selector and CIDR helpers for the corpus and live-input gates.

Exit contract: 0 clean, 1 violations, 2 unreadable input or invocation.
Vendor this file alongside any gate that imports it.
"""
from __future__ import annotations

import ipaddress
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULT_NAMESPACE = "default"
# The apiserver stamps this label on every Namespace with the namespace's name.
NAME_LABEL = "kubernetes.io/metadata.name"

# The cluster-identity ConfigMap every post-sources Flux stage substitutes from.
CLUSTER_CONFIG = "kubernetes/infrastructure/sources/cluster-config.yaml"

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")

EMPTY_CORPUS_MESSAGE = (
    "empty corpus — no manifests on stdin. A gate that passes on nothing is not "
    "a gate; check the pipe and the `kustomize build` paths feeding it."
)

EMPTY_LIVE_INPUT_MESSAGE = (
    "no {what} on stdin. A gate that checks nothing is not a gate; check the "
    "kubectl on the left of the pipe (a wrong context or namespace selector "
    "returns a valid, empty item list)."
)

SELECTOR_KEYS = {"matchLabels", "matchExpressions"}

# One Alertmanager matcher, value quoted or bare; an entry may hold several.
_AM_MATCHER = re.compile(
    r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!=|!~|=)\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s{}"]*))\s*,?'
)


class OperatorError(RuntimeError):
    """Input or invocation the gate cannot act on — exit 2, never exit 1."""


def load_docs(stream) -> list[dict]:
    """Every mapping document on `stream`, flattening `kind: List` and bare lists."""
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
        raise OperatorError(f"failed to parse YAML input: {exc}") from exc
    return docs


def load_corpus(stream=None) -> list[dict]:
    """Documents on `stream`, or exit 2 on a parse failure or an empty corpus."""
    try:
        docs = load_docs(sys.stdin if stream is None else stream)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not docs:
        print(f"ERROR: {EMPTY_CORPUS_MESSAGE}", file=sys.stderr)
        raise SystemExit(2)
    return docs


def load_live_items(stream=None, what: str = "objects") -> list[dict]:
    """Items from a `kubectl get -o json` payload on `stream`, or exit 2.

    A valid, empty item list is an operator error, not a clean cluster: the
    wrong context or selector returns one and every check then reports nothing.
    """
    try:
        payload = json.load(sys.stdin if stream is None else stream)
    except json.JSONDecodeError as exc:
        print(f"ERROR: failed to parse `kubectl get -o json` input: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        print(
            f"ERROR: input is not a {what} list (expected `kubectl get ... -o json`)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    items = [i for i in items if isinstance(i, dict)]
    if not items:
        print(f"ERROR: {EMPTY_LIVE_INPUT_MESSAGE.format(what=what)}", file=sys.stderr)
        raise SystemExit(2)
    return items


def load_cluster_config(root: Path | str = ".", path: str = CLUSTER_CONFIG) -> dict[str, str]:
    """The cluster-config ConfigMap's `data:` map, every value coerced to str.

    Raises OperatorError on an unreadable file or an empty map, so a gate whose
    key set came from here exits 2 rather than passing having checked nothing.
    """
    config_path = Path(root) / path
    try:
        docs = load_docs(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise OperatorError(f"{path} could not be read: {exc}") from exc
    except OperatorError as exc:
        raise OperatorError(f"{path}: {exc}") from exc
    for doc in docs:
        if doc.get("kind") not in (None, "ConfigMap"):
            continue
        data = doc.get("data")
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items()}
    raise OperatorError(f"no data keys in {path} — the gate has no key set to check")


def substitute(value: object, config: dict[str, str]) -> str:
    """Resolve a whole-value `${name}` placeholder the way Flux's postBuild does.

    Only a value that is nothing but one placeholder resolves; an unknown name
    and any partial spelling are returned untouched, to fail the caller's parse.
    """
    text = str(value if value is not None else "")
    match = _PLACEHOLDER.fullmatch(text.strip())
    if match and match.group(1) in config:
        return config[match.group(1)]
    return text


def doc_namespace(doc: dict) -> str:
    """The namespace a document lands in. An omitted one IS `default` to the API."""
    return (doc.get("metadata") or {}).get("namespace") or DEFAULT_NAMESPACE


def doc_key(doc: dict) -> str:
    """`namespace/Kind/name` — the one spelling every gate's allowlists use."""
    meta = doc.get("metadata") or {}
    return f"{doc_namespace(doc)}/{doc.get('kind')}/{meta.get('name', '?')}"


def api_group(doc: dict) -> str:
    """The group half of `apiVersion`; the core group (`v1`) is the empty string."""
    version = str(doc.get("apiVersion") or "")
    return version.rpartition("/")[0] if "/" in version else ""


def policy_types(spec: dict) -> set[str]:
    """A NetworkPolicy's policyTypes, inferring an absent field as the API does.

    The field is omitempty, so an empty list round-trips as an absent one and
    takes the same inferred default.
    """
    declared = spec.get("policyTypes")
    if isinstance(declared, list) and declared:
        return {str(t) for t in declared}
    types = {"Ingress"}
    if spec.get("egress"):
        types.add("Egress")
    return types


def selects_all_pods(selector: object) -> bool:
    """True when a label selector selects everything in its scope."""
    if selector is None or selector == {}:
        return True
    if not isinstance(selector, dict):
        return False
    # Unknown keys first: a `matchLables:` typo leaves the recognised terms empty.
    if set(selector) - SELECTOR_KEYS:
        return False
    labels = selector.get("matchLabels")
    exprs = selector.get("matchExpressions")
    # Typed, not truthy: API-invalid terms neither fence nor defeat a fence.
    if labels is not None and not isinstance(labels, dict):
        return False
    if exprs is not None and not isinstance(exprs, list):
        return False
    return not (labels or exprs)


# The apiserver's label validation: an optional DNS-subdomain prefix, then a
# 63-char alphanumeric-bounded name; a value is that name shape or empty.
_LABEL_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
_LABEL_PREFIX = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_EXPRESSION_KEYS = {"key", "operator", "values"}
_OPERATORS = ("In", "NotIn", "Exists", "DoesNotExist")

# A whole Flux postBuild placeholder, which an unrendered tree carries where
# substitution later writes a label value.
_WHOLE_PLACEHOLDER = re.compile(r"\$\{[^{}]+\}")


def label_key_is_valid(key: object) -> bool:
    """Whether `key` is a label key the apiserver accepts."""
    if not isinstance(key, str) or not key:
        return False
    prefix, slash, name = key.rpartition("/")
    if slash and (not prefix or len(prefix) > 253 or not _LABEL_PREFIX.fullmatch(prefix)):
        return False
    return bool(_LABEL_NAME.fullmatch(name))


def label_value_is_valid(value: object, *, placeholders: bool = False) -> bool:
    """Whether `value` is a label value the apiserver accepts.

    With `placeholders`, a value that is one whole `${...}` also passes.
    """
    if not isinstance(value, str):
        return False
    if value == "" or _LABEL_NAME.fullmatch(value):
        return True
    return placeholders and bool(_WHOLE_PLACEHOLDER.fullmatch(value))


def label_selector_problem(selector: object, *, placeholders: bool = False) -> str | None:
    """Why the apiserver would reject a LabelSelector, or None. Absent is valid.

    Known keys and operators, typed terms, valid label keys and values, and a
    non-empty value list for In/NotIn but none for Exists/DoesNotExist.
    """
    if selector is None:
        return None
    if not isinstance(selector, dict):
        return "is not a mapping"
    unknown = sorted(str(k) for k in set(selector) - SELECTOR_KEYS)
    if unknown:
        return f"carries {', '.join(unknown)}"
    labels = selector.get("matchLabels")
    if labels is not None and not isinstance(labels, dict):
        return "has a matchLabels that is not a mapping"
    for key, value in (labels or {}).items():
        if not label_key_is_valid(key):
            return f"has a matchLabels key {key!r} that is not a valid label key"
        if not label_value_is_valid(value, placeholders=placeholders):
            return f"has a matchLabels value {value!r} that is not a valid label value"
    exprs = selector.get("matchExpressions")
    if exprs is not None and not isinstance(exprs, list):
        return "has a matchExpressions that is not a list"
    for expr in exprs or []:
        if (
            not isinstance(expr, dict)
            or set(expr) - _EXPRESSION_KEYS
            or not label_key_is_valid(expr.get("key"))
        ):
            return f"has a malformed matchExpressions entry {expr!r}"
        operator, values = expr.get("operator"), expr.get("values")
        if operator not in _OPERATORS:
            return f"has a matchExpressions operator {operator!r} this gate does not know"
        if operator in ("In", "NotIn"):
            wellformed = isinstance(values, list) and bool(values) and all(
                label_value_is_valid(v, placeholders=placeholders) for v in values
            )
        else:
            wellformed = values in (None, [])
        if not wellformed:
            return f"has a malformed matchExpressions entry {expr!r}"
    return None


def selector_matches(selector: object, labels: dict, *, placeholders: bool = False) -> bool:
    """Kubernetes LabelSelector semantics: matchLabels AND every matchExpressions term.

    An absent selector and one the apiserver would reject match nothing, so a
    gate never credits a match the cluster would not make. `{}` matches all.
    """
    if not isinstance(selector, dict) or label_selector_problem(
        selector, placeholders=placeholders
    ):
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if key not in labels or labels[key] != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, operator = expr["key"], expr["operator"]
        present = key in labels
        listed = present and labels[key] in (expr.get("values") or [])
        # NotIn and DoesNotExist hold for a label set that lacks the key.
        if (
            (operator == "In" and not listed)
            or (operator == "NotIn" and listed)
            or (operator == "Exists" and not present)
            or (operator == "DoesNotExist" and present)
        ):
            return False
    return True


def condition_admits(
    condition: object,
    namespace: str,
    labels: dict,
    bad_patterns: list[tuple[str, re.error]] | None = None,
) -> bool:
    """Whether one ESO store condition admits a namespace with these labels.

    An uncompilable namespaceRegexes entry admits nothing and is appended to
    `bad_patterns` when the caller collects them.
    """
    if not isinstance(condition, dict):
        return False
    namespaces = condition.get("namespaces")
    if isinstance(namespaces, list) and namespace in namespaces:
        return True
    patterns = condition.get("namespaceRegexes")
    for pattern in patterns if isinstance(patterns, list) else []:
        try:
            if re.search(str(pattern), namespace):
                return True
        except re.error as exc:
            if bad_patterns is not None:
                bad_patterns.append((str(pattern), exc))
            continue
    selector = condition.get("namespaceSelector")
    return selector is not None and selector_matches(selector, labels)


def zero_prefix(cidr: object) -> bool:
    """Whether a CIDR's prefix length is 0 — numerically, so `/00` counts."""
    _address, separator, prefix_length = str(cidr or "").strip().rpartition("/")
    try:
        return separator == "/" and int(prefix_length, 10) == 0
    except ValueError:
        return False


def exclude_nets(nets: list, cuts: list) -> list:
    """The parts of `nets` no entry of `cuts` covers.

    CIDRs nest or are disjoint, so each net is kept, dropped, or split. A cut of
    another address family covers nothing and passes the net through.
    """
    remaining = list(nets)
    for cut in cuts:
        surviving = []
        for part in remaining:
            if part.version != cut.version:
                surviving.append(part)
            elif part.subnet_of(cut):
                continue
            elif cut.subnet_of(part):
                surviving.extend(part.address_exclude(cut))
            else:
                surviving.append(part)
        remaining = surviving
        if not remaining:
            return remaining
    return remaining


def excepts_cover_cidr(excepts: object, cidr: object) -> bool:
    """True only when the except list leaves no address of `cidr` admitted.

    Exact subtraction, not a well-known-ranges heuristic: pod CIDRs vary by
    cluster. An entry the API would reject never counts toward coverage.
    """
    try:
        net = ipaddress.ip_network(str(cidr or "").strip(), strict=False)
    except ValueError:
        return False
    cuts = []
    for raw in excepts if isinstance(excepts, list) else []:
        try:
            exc = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            continue
        # Each entry must be a strict subnet of the cidr, so an equal or
        # out-of-range one is API-invalid and credits no coverage.
        if exc.version != net.version or exc == net or not exc.subnet_of(net):
            continue
        cuts.append(exc)
    return not exclude_nets([net], cuts)


def peer_selects_everything(peer: object) -> bool:
    """True when one NetworkPolicy peer narrows nothing."""
    if not isinstance(peer, dict):
        return False
    ip_block = peer.get("ipBlock")
    if ip_block is not None:
        if not isinstance(ip_block, dict):
            return False
        if not zero_prefix(ip_block.get("cidr")):
            return False
        return not excepts_cover_cidr(ip_block.get("except"), ip_block.get("cidr"))
    # An omitted namespaceSelector scopes the peer to the policy's own namespace,
    # so it narrows even when the podSelector is empty.
    if peer.get("namespaceSelector") is None and "podSelector" in peer:
        return False
    for key in ("namespaceSelector", "podSelector"):
        if not selects_all_pods(peer.get(key)):
            return False
    return True


def parse_exempt(values: list[str]) -> dict[str, str]:
    """`NS=REASON` pairs. A reason is mandatory: an unexplained exemption is a hole."""
    exempt: dict[str, str] = {}
    for raw in values or []:
        ns, sep, reason = raw.partition("=")
        if not sep or not ns.strip() or not reason.strip():
            raise OperatorError(f"--exempt takes NS=REASON, got {raw!r}")
        exempt[ns.strip()] = reason.strip()
    return exempt


def load_exempt(values: list[str], files: list[str]) -> dict[str, str]:
    """`namespace/Kind/name=REASON` flags plus YAML mappings of the same."""
    exempt: dict[str, str] = {}
    for raw in values or []:
        key, sep, reason = raw.partition("=")
        if not sep or key.strip().count("/") != 2 or not reason.strip():
            raise OperatorError(f"--exempt takes namespace/Kind/name=REASON, got {raw!r}")
        exempt[key.strip()] = reason.strip()
    for path in files or []:
        try:
            docs = load_docs(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, OperatorError) as exc:
            raise OperatorError(f"--exempt-file {path}: {exc}") from exc
        for doc in docs:
            for key, reason in doc.items():
                if str(key).count("/") != 2 or not str(reason or "").strip():
                    raise OperatorError(
                        f"--exempt-file {path}: {key!r} needs the namespace/Kind/name "
                        f"form and a reason"
                    )
                exempt[str(key)] = str(reason).strip()
    return exempt


def report_exemptions(exempt: dict[str, str], used: set[str]) -> None:
    """Print each exercised exemption with its reason, then the unexercised keys."""
    for key in sorted(used):
        print(f"  {key}: exempt ({exempt[key]})")
    unused = sorted(set(exempt) - used)
    if unused:
        print(f"(exemptions declared but not exercised by this corpus: {', '.join(unused)})")


def parse_alertmanager_matchers(entry: object) -> list[tuple[str, str, str]]:
    """(label, operator, value) for every matcher in one Alertmanager matcher entry.

    Takes `a="x"`, `{a="x",b=~"y|z"}` and bare values; quoted escapes are undone.
    Raises ValueError when the entry is empty or does not parse.
    """
    text = str(entry).strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    pos, parsed = 0, []
    while pos < len(text):
        match = _AM_MATCHER.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError(f"matcher {entry!r} is unparseable")
        label, op, quoted, bare = match.groups()
        value = re.sub(r"\\(.)", r"\1", quoted) if quoted is not None else bare
        parsed.append((label, op, value))
        pos = match.end()
    if not parsed:
        raise ValueError(f"matcher {entry!r} is empty")
    return parsed
