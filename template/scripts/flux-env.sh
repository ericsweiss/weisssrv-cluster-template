#!/usr/bin/env bash
# Substitution ConfigMaps -> eval-able exports, the kubeconform schema version,
# or one merged ConfigMap. Contract: weisssrv-lib docs/SCRIPTS.md - flux-env.sh.
# Usage: VARS=$(scripts/flux-env.sh export-versions "$CMS"); eval "$VARS"
set -euo pipefail

# `-` not `:-`: an explicitly empty value means "no extras", while an unset one
# means the conventional sibling cluster-config.yaml.
export FLUX_EXTRA_CONFIGMAPS="${FLUX_EXTRA_CONFIGMAPS-kubernetes/infrastructure/sources/cluster-config.yaml}"

exec python3 - "$@" <<'PY'
import os
import re
import shlex
import sys

import yaml

# Callers `eval` the exports, so a key must be a POSIX shell name and must not
# clobber a variable the calling shell owns.
_VALID_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = {
    "PATH", "HOME", "IFS", "PWD", "SHELL", "TMPDIR", "CI",
    "CLUSTER_DIR", "SKIPPED_SCRIPT", "FLUX_RENDER_SCRIPT", "VERSIONS_CONFIGMAP",
    "FAILED", "RENDER_ALL", "K8S_VER", "VARS", "FLUX_ENVSUBST_VARS",
}
SUBCOMMANDS = ("export-versions", "k8s-version", "merged-configmap")


def die(msg: str) -> None:
    sys.exit(f"flux-env: ERROR: {msg}")


sub = sys.argv[1] if len(sys.argv) > 1 else ""
if sub not in SUBCOMMANDS:
    die(f"unknown subcommand: {sub or '<none>'} (want: {'|'.join(SUBCOMMANDS)})")
# Callers pass a space-separated list as one argument.
named = " ".join(sys.argv[2:]).split()
if not named:
    die(f"usage: flux-env.sh {sub} <configmap> [configmap ...]")
if sub == "k8s-version":
    # The k3s pin lives in the first ConfigMap.
    paths = named[:1]
else:
    paths = list(dict.fromkeys(named + os.environ["FLUX_EXTRA_CONFIGMAPS"].split()))

data = {}
for path in paths:
    if not os.path.isfile(path):
        die(f"ConfigMap not found: {path}")
    with open(path) as fh:
        entries = (yaml.safe_load(fh) or {}).get("data") or {}
    if not entries:
        die(f"no substitution keys found in {path}")
    for key, value in entries.items():
        key, value = str(key), str(value)
        if not _VALID_KEY.match(key):
            die(f"invalid shell variable name in {path}: {key}")
        if key in _RESERVED or key.endswith("_SHA256"):
            die(f"reserved variable name in {path}: {key}")
        # Callers re-read the exports line by line, and a newline cannot
        # survive envsubst into YAML either.
        if "\n" in value:
            die(f"multi-line value for {key} in {path}")
        data[key] = value

if sub == "export-versions":
    for key, value in data.items():
        print(f"export {key}={shlex.quote(value)}")
    allowlist = "".join(f"${{{key}}} " for key in data)
    print(f"export FLUX_ENVSUBST_VARS={shlex.quote(allowlist)}")
elif sub == "k8s-version":
    # No default: a missing pin would drift local lint and CI onto different
    # kubeconform schemas with nothing red.
    m = re.search(r"(\d+)\.(\d+)", data.get("k3s_version", ""))
    if not m:
        die(f"could not derive k3s_version from {paths[0]} (expected a "
            "'k3s_version: X.Y.Z' data key) — run 'task flux:sync-versions'")
    print(f"{m.group(1)}.{m.group(2)}.0")
else:
    yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "merged-substitutions"},
            "data": data,
        },
        sys.stdout,
        sort_keys=True,
    )
PY
