#!/usr/bin/env bash
# Confirmation ceremony for a supervised apply: refuses a non-tty and any
# -auto-approve spelling, then makes the operator type the confirm word.
# Usage + env: weisssrv-lib docs/SCRIPTS.md - supervised-apply-guard.sh.
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: $0 <task-name> <what-it-rewrites> [cli-args...]" >&2
    exit 2
fi

task_name=$1
what=$2
shift 2

# The word the operator types. `-` not `:-`, so an explicitly empty value
# reaches the refusal below instead of silently taking the default.
confirm_word="${SUPERVISED_APPLY_CONFIRM_WORD-apply}"
if [ -z "$confirm_word" ]; then
    echo "ERROR: SUPERVISED_APPLY_CONFIRM_WORD is empty; a bare Enter would approve." >&2
    exit 2
fi

if [ ! -t 0 ]; then
    echo "ERROR: $task_name is a supervised operator step; run it from a terminal." >&2
    exit 2
fi

# Unanchored on purpose: a task runner shell-quotes each CLI arg, so a
# space-delimited pattern misses the equally valid `-auto-approve=true` and
# `--auto-approve`.
case " $* " in
    *auto-approve*)
        echo "ERROR: -auto-approve is refused; $task_name is supervised. Review the plan and confirm at the prompt." >&2
        exit 2
        ;;
esac

echo "About to run $task_name — this rewrites $what."
printf 'Type "%s" to continue: ' "$confirm_word"
read -r confirm
[ "$confirm" = "$confirm_word" ] || {
    echo "Aborted."
    exit 1
}
