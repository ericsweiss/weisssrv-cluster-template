#!/usr/bin/env python3
"""Assert every Traefik route to an internal-domain host names a source allowlist.

Reads the rendered corpus on stdin; exits 0 clean, 1 on a finding, 2 on an
operator error. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this script's own directory)
        OperatorError,
        doc_key,
        doc_namespace,
        load_cluster_config,
        load_corpus,
        load_exempt,
        report_exemptions,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

ROUTE_KINDS = ("IngressRoute", "IngressRouteTCP")
DEFAULT_DOMAIN_KEY = "cluster_internal_domain"
CRD_PROVIDER = "kubernetescrd"
_REQUIRED_FORMAT = "--required-middleware takes NAMESPACE/NAME or NAME@PROVIDER"

# Every Traefik matcher that names a host, v2 and v3 spellings alike.
_HOST_CALL = re.compile(r"\b(HostSNIRegexp|HostSNI|HostRegexp|HostHeader|Host)\s*\(([^)]*)\)")
_HOST_ARG = re.compile(r"`([^`]*)`|\"([^\"]*)\"")
# A dotted name ending in a label that starts with a letter: what a generic
# regexp such as `.+` or `^[a-z]+$` never spells.
_DOMAIN_LITERAL = re.compile(r"[a-z0-9-]+\.[a-z][a-z0-9-]*")


class Domains:
    """The internal domain in every spelling a corpus may carry it."""

    def __init__(self, literals: list[str], placeholder: str) -> None:
        self.literals = sorted({d.strip().lower().lstrip("*.") for d in literals if d.strip()})
        self.placeholder = placeholder

    def matches_host(self, host: str) -> bool:
        """An exact host on the domain, a subdomain of it, or its placeholder."""
        host = host.strip().lower()
        if self.placeholder.lower() in host:
            return True
        return any(host == d or host.endswith("." + d) for d in self.literals)

    def matches_regexp(self, pattern: str) -> bool:
        """A host regexp that spells the domain, escaped or not."""
        if self.placeholder in pattern:
            return True
        plain = pattern.replace("\\.", ".").lower()
        return any(re.search(r"(?<![a-z0-9-])" + re.escape(d), plain) for d in self.literals)


def _host_args(match: object):
    """Yield `(is_regexp, host)` for every host argument in a route `match` expression."""
    for call in _HOST_CALL.finditer(str(match or "")):
        regexp = call.group(1).endswith("Regexp")
        for arg in _HOST_ARG.finditer(call.group(2)):
            yield regexp, arg.group(1) if arg.group(1) is not None else arg.group(2)


def internal_hosts(match: object, domains: Domains) -> list[str]:
    """The host arguments in one route `match` expression on the internal domain."""
    return [
        host for regexp, host in _host_args(match)
        if (domains.matches_regexp(host) if regexp else domains.matches_host(host))
    ]


def names_a_host(match: object) -> bool:
    """True when a `match` expression pins a host, not a wildcard or a generic regexp."""
    for regexp, host in _host_args(match):
        host = host.strip()
        if regexp:
            if "${" in host or _DOMAIN_LITERAL.search(host.replace("\\.", ".").lower()):
                return True
        elif host not in ("", "*"):
            return True
    return False


def parse_required(values: list[str]) -> set[str]:
    """`NAMESPACE/NAME` or `NAME@PROVIDER` references, normalised to one spelling."""
    required: set[str] = set()
    for raw in values or []:
        text = raw.strip()
        if "@" in text:
            name, _, provider = text.partition("@")
            if not name or not provider:
                raise OperatorError(f"{_REQUIRED_FORMAT}, got {raw!r}")
            required.add(text)
            continue
        namespace, slash, name = text.partition("/")
        if not (slash and namespace and name) or "/" in name:
            raise OperatorError(f"{_REQUIRED_FORMAT}, got {raw!r}")
        required.add(f"{namespace}/{name}")
    if not required:
        raise OperatorError("no --required-middleware given — every internal route would fail")
    return required


def ref_spellings(ref: dict, own_namespace: str) -> set[str]:
    """Every spelling one middleware reference resolves to.

    A `name@kubernetescrd` reference is Traefik's `<namespace>-<name>` form, which
    is ambiguous on hyphens, so it is compared as written.
    """
    name = str(ref.get("name") or "").strip()
    if not name:
        return set()
    if "@" in name:
        return {name}
    namespace = str(ref.get("namespace") or own_namespace).strip()
    return {f"{namespace}/{name}", f"{namespace}-{name}@{CRD_PROVIDER}"}


def display(ref: dict, own_namespace: str) -> str:
    """One reference as a reader would write it."""
    name = str(ref.get("name") or "").strip()
    if not name or "@" in name:
        return name
    return f"{ref.get('namespace') or own_namespace}/{name}"


def required_spellings(required: set[str]) -> set[str]:
    """Each NAMESPACE/NAME requirement also in its `@kubernetescrd` spelling."""
    out = set(required)
    for item in required:
        if "@" not in item:
            namespace, _, name = item.partition("/")
            out.add(f"{namespace}-{name}@{CRD_PROVIDER}")
    return out


def chains(docs: list[dict]) -> dict[str, list[set[str]]]:
    """`namespace/name` of every chain Middleware -> the spellings of its members."""
    out: dict[str, list[set[str]]] = {}
    for doc in docs:
        if doc.get("kind") != "Middleware":
            continue
        chain = ((doc.get("spec") or {}).get("chain") or {})
        members = chain.get("middlewares") if isinstance(chain, dict) else None
        if not isinstance(members, list):
            continue
        namespace = doc_namespace(doc)
        name = (doc.get("metadata") or {}).get("name")
        out[f"{namespace}/{name}"] = [
            ref_spellings(m, namespace) for m in members if isinstance(m, dict)
        ]
    return out


def credits(spellings: set[str], allowed: set[str], chain_map: dict, seen: set[str]) -> bool:
    """True when a reference is an allowlist, or a chain that contains one."""
    if spellings & allowed:
        return True
    for spelling in sorted(spellings):
        if spelling in chain_map and spelling not in seen:
            seen.add(spelling)
            if any(credits(m, allowed, chain_map, seen) for m in chain_map[spelling]):
                return True
    return False


def findings(
    docs: list[dict], domains: Domains, required: set[str], exempt: dict[str, str]
) -> tuple[list[str], int, int, set[str]]:
    """-> (violations, internal entries checked, catch-all entries, exemptions exercised).

    A host-less IngressRoute matches the internal hostnames too and is checked;
    a host-less IngressRouteTCP route, such as `HostSNI(`*`)`, is out of scope.
    """
    allowed = required_spellings(required)
    chain_map = chains(docs)
    out: list[str] = []
    checked = 0
    catch_all = 0
    used: set[str] = set()
    for doc in docs:
        if doc.get("kind") not in ROUTE_KINDS:
            continue
        key = doc_key(doc)
        namespace = doc_namespace(doc)
        for index, route in enumerate((doc.get("spec") or {}).get("routes") or []):
            if not isinstance(route, dict):
                continue
            hosts = internal_hosts(route.get("match"), domains)
            if hosts:
                checked += 1
            elif names_a_host(route.get("match")) or doc.get("kind") == "IngressRouteTCP":
                continue
            else:
                catch_all += 1
            if key in exempt:
                used.add(key)
                continue
            refs = [m for m in route.get("middlewares") or [] if isinstance(m, dict)]
            if any(credits(ref_spellings(r, namespace), allowed, chain_map, set()) for r in refs):
                continue
            named = ", ".join(sorted(filter(None, (display(r, namespace) for r in refs))))
            matched = ", ".join(hosts) or "no host matcher, so every host"
            out.append(
                f"  {key} routes[{index}] ({matched}): names no source "
                f"allowlist middleware (has: {named or 'none'}; needs one of "
                f"{', '.join(sorted(required))}). Both VIPs share the entrypoints, "
                f"so the public VIP serves this host too."
            )
    return out, checked, catch_all, used


def resolve_domains(args) -> Domains:
    """The literal domains from flags and the cluster-config, plus the placeholder."""
    literals = list(args.internal_domain)
    if args.cluster_config:
        config = load_cluster_config(".", args.cluster_config)
        value = config.get(args.domain_key)
        if not value:
            raise OperatorError(f"{args.cluster_config} has no {args.domain_key} key")
        literals.append(value)
    if not literals:
        raise OperatorError("give --internal-domain or --cluster-config: the gate has no domain")
    return Domains(literals, "${%s}" % args.domain_key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Every internal-domain Traefik route must name a source allowlist."
    )
    parser.add_argument(
        "--internal-domain", action="append", default=[], metavar="DOMAIN",
        help="internal domain; repeatable",
    )
    parser.add_argument(
        "--cluster-config", metavar="PATH",
        help="cluster-config ConfigMap to read the internal domain from",
    )
    parser.add_argument(
        "--domain-key", default=DEFAULT_DOMAIN_KEY,
        help="ConfigMap key and ${placeholder} name of the internal domain",
    )
    parser.add_argument(
        "--required-middleware", action="append", default=[],
        metavar="NAMESPACE/NAME",
        help="an allowlist middleware that satisfies the gate; repeatable",
    )
    parser.add_argument(
        "--exempt", action="append", default=[], metavar="NAMESPACE/KIND/NAME=REASON",
        help="a route exempt from the rule, with its reason; repeatable",
    )
    parser.add_argument(
        "--exempt-file", action="append", default=[], metavar="PATH",
        help="YAML mapping of namespace/Kind/name to reason; repeatable",
    )
    args = parser.parse_args(argv)
    try:
        required = parse_required(args.required_middleware)
        exempt = load_exempt(args.exempt, args.exempt_file)
        domains = resolve_domains(args)
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    docs = load_corpus()
    found, checked, catch_all, used = findings(docs, domains, required, exempt)
    if found:
        print("Internal-domain routes reachable from the public VIP:", file=sys.stderr)
        print("\n".join(found), file=sys.stderr)
        return 1
    if not checked:
        print(
            f"ERROR: no IngressRoute or IngressRouteTCP in {len(docs)} document(s) "
            f"matches the internal domain — check the domain flags and the render "
            f"paths feeding stdin.",
            file=sys.stderr,
        )
        return 2
    report_exemptions(exempt, used)
    print(
        f"internal route allowlist OK — {checked} internal route(s) and {catch_all} "
        f"host-less route(s) in {len(docs)} document(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
