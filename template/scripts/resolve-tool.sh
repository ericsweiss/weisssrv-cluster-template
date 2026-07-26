#!/usr/bin/env bash
# Resolve how to invoke a Python-based dev tool, printing the invocation to
# stdout (may be multi-word, e.g. "python3 -m molecule", or an absolute pyenv
# path) or exiting 1 if it can't be found. One place for the command-v /
# python3 -m / validated-pyenv-glob chain callers would otherwise each repeat.
#
# Usage: resolve-tool.sh <tool> [python-module]
#   resolve-tool.sh molecule molecule    -> may print "python3 -m molecule"
#   resolve-tool.sh ansible-lint         -> command-v or pyenv only
#
# Callers invoke the result UNQUOTED so a multi-word form word-splits:
#   MOL=$(scripts/resolve-tool.sh molecule molecule) || exit 1
#   $MOL test
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
