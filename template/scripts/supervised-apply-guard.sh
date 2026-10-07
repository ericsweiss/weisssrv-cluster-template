#!/usr/bin/env bash
# Guard for a supervised `terraform apply`: requires a terminal, refuses
# -auto-approve, and makes the operator type `apply`. Called as the first cmds
# entry of the task so `read` gets the task's stdin.
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: $0 <task-name> <what-it-rewrites> [cli-args...]" >&2
    exit 2
fi

task_name=$1
what=$2
shift 2

if [ ! -t 0 ]; then
    echo "ERROR: $task_name is a supervised operator step; run it from a terminal." >&2
    exit 2
fi

# Unanchored on purpose: Task shell-quotes each CLI arg, so a space-delimited
# pattern misses the equally valid `-auto-approve=true` and `--auto-approve`.
case " $* " in
    *auto-approve*)
        echo "Refusing -auto-approve: $task_name is supervised. Review the plan and confirm at the prompt." >&2
        exit 2
        ;;
esac

echo "About to run $task_name — this rewrites $what."
printf 'Type "apply" to continue: '
read -r confirm
[ "$confirm" = "apply" ] || {
    echo "Aborted."
    exit 1
}
