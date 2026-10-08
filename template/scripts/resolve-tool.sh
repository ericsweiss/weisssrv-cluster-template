#!/usr/bin/env bash
# Print how to invoke a Python dev tool (`resolve-tool.sh <tool> [module]`), exit
# 1 when not found. Multi-word, so callers use it UNQUOTED: `$MOL test`.
# Contract: weisssrv-lib docs/SCRIPTS.md - resolve-tool.sh.
set -euo pipefail

tool="${1:-}"
module="${2:-}"
[ -n "$tool" ] || { echo "Usage: $0 <tool> [python-module]" >&2; exit 2; }

# 1. On PATH.
if command -v "$tool" >/dev/null 2>&1; then
    echo "$tool"
    exit 0
fi

# 2. As a python3 module — only when a module name is given. molecule's module
#    imports as `molecule`, but ansible-lint's is `ansiblelint`, so callers that
#    don't want this step simply omit the module arg.
if [ -n "$module" ] && python3 -m "$module" --version >/dev/null 2>&1; then
    echo "python3 -m $module"
    exit 0
fi

# 3. pyenv installs — validate each candidate actually runs before selecting it
#    (a broken/partial install must not shadow a working one).
for candidate in "$HOME"/.pyenv/versions/*/bin/"$tool"; do
    if [ -x "$candidate" ] && "$candidate" --version >/dev/null 2>&1; then
        echo "$candidate"
        exit 0
    fi
done

exit 1
