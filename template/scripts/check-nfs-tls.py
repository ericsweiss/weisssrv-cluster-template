#!/usr/bin/env python3
"""Assert every NFS PersistentVolume mounts over TLS, by hostname.

Covers `spec.nfs` on a PersistentVolume only, reading the rendered corpus on
stdin. Scope, inputs and exit codes: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path
from typing import List, Tuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from gate_common import (  # noqa: E402  (resolved from this script's own directory)
        doc_key,
        load_corpus,
    )
except ImportError:
    print(
        "ERROR: gate_common.py must be vendored beside this gate "
        "(see scripts/vendorable-paths.yml)", file=sys.stderr,
    )
    raise SystemExit(2) from None

DEFAULT_OPTION = "xprtsec=tls"


def _is_ip(server: str) -> bool:
    try:
        ipaddress.ip_address(server)
    except ValueError:
        return False
    return True


def nfs_violations(docs: List[dict], required_option: str = DEFAULT_OPTION,
                   cert_domain: str = "",
                   allow_ip_server: bool = False) -> Tuple[List[str], int]:
    """-> (violations, NFS PVs inspected). The count feeds the vacuity guard."""
    out: List[str] = []
    seen = 0
    certificate = "%s certificate" % cert_domain if cert_domain else "server certificate"
    for doc in docs:
        if doc.get("kind") != "PersistentVolume":
            continue
        spec = doc.get("spec") or {}
        nfs = spec.get("nfs")
        if not isinstance(nfs, dict):
            continue
        seen += 1
        name = doc_key(doc)
        options = [str(o) for o in (spec.get("mountOptions") or [])]
        # Kubernetes comma-joins mountOptions for the mount helper, so one
        # element may carry several options.
        flat = {part.strip() for element in options for part in element.split(",")}
        if required_option not in flat:
            out.append(
                "%s: mountOptions lack %s (has %s) — the export "
                "rejects plaintext" % (name, required_option, options or "none")
            )
        server = str(nfs.get("server", ""))
        if not server:
            out.append("%s: spec.nfs.server is empty" % name)
        elif _is_ip(server) and not allow_ip_server:
            out.append(
                "%s: server %s is an IP — the %s has no IP SAN, "
                "so the TLS handshake fails" % (name, server, certificate)
            )
    return out, seen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--required-option", default=DEFAULT_OPTION)
    parser.add_argument(
        "--cert-domain", default="",
        help="wildcard cert domain, named in the IP-server message",
    )
    parser.add_argument(
        "--allow-ip-server", action="store_true",
        help="the server certificate carries an IP SAN, so an IP in "
             "spec.nfs.server is allowed",
    )
    parser.add_argument(
        "--allow-empty", action="store_true",
        help="a corpus with no NFS PersistentVolume is a pass, for a consumer "
             "with no NFS storage",
    )
    args = parser.parse_args(argv)

    docs = load_corpus()

    found, seen = nfs_violations(
        docs, args.required_option, args.cert_domain, args.allow_ip_server
    )
    if found:
        print(
            "ERROR: NFS PersistentVolumes that cannot mount against the TLS-only "
            "exports:", file=sys.stderr,
        )
        print("\n".join(found), file=sys.stderr)
        return 1

    if not seen and not args.allow_empty:
        print(
            "ERROR: inspected 0 NFS PersistentVolumes in %d document(s) — check "
            "that the `kustomize build` paths feeding stdin cover the stages that "
            "declare NFS storage, or pass --allow-empty if this cluster has none."
            % len(docs), file=sys.stderr,
        )
        return 2

    print(
        "NFS TLS policy OK — %d NFS PersistentVolume(s) across %d document(s) "
        "(every one mounts %s%s)"
        % (seen, len(docs), args.required_option,
           "" if args.allow_ip_server else " by hostname")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
