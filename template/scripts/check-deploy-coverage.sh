#!/usr/bin/env bash
# Fail when a changed Ansible role, playbook or inventory file matches no deploy
# job's `changes:` list, or when a job lists a literal path that does not exist.
# Usage: [BASE_REF]; $DEPLOY_COVERAGE_CONFIG; contract in docs/SCRIPTS.md.

set -euo pipefail

# ci_yaml.py is imported from this script's own directory: vendor both.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export SCRIPT_DIR

CONFIG="${DEPLOY_COVERAGE_CONFIG:-scripts/deploy-coverage.conf}"

ROLES_DIR="ansible/roles"
PLAYBOOKS_DIR="ansible/playbooks"
INVENTORY_DIR="ansible/inventories/prod"
CI_FILE=".gitlab-ci.yml"
JOB_PREFIX="deploy-"
JOB_STAGE="deploy"

INTENTIONALLY_UNMAPPED_ROLES=()
INTENTIONALLY_UNMAPPED_PLAYBOOKS=()
INTENTIONALLY_UNMAPPED_INVENTORY_PATHS=()

if [ -f "$CONFIG" ]; then
    section=""
    lineno=0
    while IFS= read -r raw || [ -n "$raw" ]; do
        lineno=$((lineno + 1))
        line="${raw%%$'\r'}"
        # Trim leading/trailing whitespace.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [ -z "$line" ] && continue
        [ "${line:0:1}" = "#" ] && continue
        if [[ "$line" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        value="${line%%#*}"
        rationale="${line#*#}"
        value="${value%"${value##*[![:space:]]}"}"
        if [ "$section" = "settings" ]; then
            key="${value%%=*}"
            val="${value#*=}"
            key="${key%"${key##*[![:space:]]}"}"
            val="${val#"${val%%[![:space:]]*}"}"
            case "$key" in
                roles_dir) ROLES_DIR="$val" ;;
                playbooks_dir) PLAYBOOKS_DIR="$val" ;;
                inventory_dir) INVENTORY_DIR="$val" ;;
                ci_file) CI_FILE="$val" ;;
                job_prefix) JOB_PREFIX="$val" ;;
                job_stage) JOB_STAGE="$val" ;;
                *) echo "ERROR: $CONFIG:$lineno: unknown setting '$key'" >&2; exit 2 ;;
            esac
            continue
        fi
        # Rationale enforcement: an entry with no trailing comment is rejected.
        if [ "$rationale" = "$line" ] || [ -z "${rationale//[[:space:]]/}" ]; then
            echo "ERROR: $CONFIG:$lineno: entry '$value' has no '# rationale' comment" >&2
            echo "       Every intentionally-unmapped entry must say why it is not" >&2
            echo "       wired to a deploy job and what deploys it instead." >&2
            exit 2
        fi
        case "$section" in
            roles) INTENTIONALLY_UNMAPPED_ROLES+=("$value") ;;
            playbooks) INTENTIONALLY_UNMAPPED_PLAYBOOKS+=("$value") ;;
            inventory) INTENTIONALLY_UNMAPPED_INVENTORY_PATHS+=("$value") ;;
            "") echo "ERROR: $CONFIG:$lineno: entry before any [section]" >&2; exit 2 ;;
            *) echo "ERROR: $CONFIG:$lineno: unknown section '[$section]'" >&2; exit 2 ;;
        esac
    done < "$CONFIG"
fi

# ERE-safe forms of the configured directories.
ere() { printf '%s' "${1//./\\.}"; }
ROLES_ERE=$(ere "$ROLES_DIR")
PLAYBOOKS_ERE=$(ere "$PLAYBOOKS_DIR")
INVENTORY_ERE=$(ere "$INVENTORY_DIR")

# Diff base, in priority order: CI_MERGE_REQUEST_DIFF_BASE_SHA, then $1, then
# CI_COMMIT_BEFORE_SHA (all-zeros on a brand-new branch means "no base"), then
# origin/main.
BASE_REF="${CI_MERGE_REQUEST_DIFF_BASE_SHA:-}"
[ -z "$BASE_REF" ] && BASE_REF="${1:-}"
if [ -z "$BASE_REF" ]; then
    if [ -n "${CI_COMMIT_BEFORE_SHA:-}" ] && [ "$CI_COMMIT_BEFORE_SHA" != "0000000000000000000000000000000000000000" ]; then
        BASE_REF="$CI_COMMIT_BEFORE_SHA"
    else
        BASE_REF="origin/main"
    fi
fi

# An unresolvable BASE_REF is an error, never an empty change set.
if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
    {
        echo "ERROR: BASE_REF '$BASE_REF' is not a valid git ref or commit."
        echo "       Set CI_MERGE_REQUEST_DIFF_BASE_SHA, pass a valid ref as \$1,"
        echo "       or ensure 'origin/main' is fetched in this checkout."
    } >&2
    exit 2
fi

# Unrelated histories produce an empty three-dot diff, which would read as
# "nothing changed"; require a common ancestor instead.
if ! git merge-base --is-ancestor "$BASE_REF" HEAD 2>/dev/null \
   && ! git merge-base "$BASE_REF" HEAD >/dev/null 2>&1; then
    {
        echo "ERROR: BASE_REF '$BASE_REF' shares no common ancestor with HEAD."
        echo "       This is usually a shallow-clone problem in CI (the MR base"
        echo "       commit isn't in the local history). Fetch deeper or unshallow."
    } >&2
    exit 2
fi

# --diff-filter=d drops deletions; a removed asset has nothing to roll out.
DIFF_FILES=$(git diff --name-only --diff-filter=d "$BASE_REF"...HEAD)

CHANGED_ROLES=$(
    printf '%s\n' "$DIFF_FILES" \
        | grep -oE "^${ROLES_ERE}/[A-Za-z0-9_-]+" \
        | sed "s|^${ROLES_DIR}/||" \
        | sort -u \
        || true
)

# Playbook identifier is the path relative to <playbooks_dir>/.
CHANGED_PLAYBOOKS=$(
    printf '%s\n' "$DIFF_FILES" \
        | grep -E "^${PLAYBOOKS_ERE}/.+\.ya?ml$" \
        | sed "s|^${PLAYBOOKS_DIR}/||" \
        | sort -u \
        || true
)

# Any *.yml/*.yaml under <inventory_dir>/ at any depth, identified relative to it.
CHANGED_INVENTORY_PATHS=$(
    printf '%s\n' "$DIFF_FILES" \
        | grep -E "^${INVENTORY_ERE}/.+\.ya?ml$" \
        | sed "s|^${INVENTORY_DIR}/||" \
        | sort -u \
        || true
)

# Line-by-line: a path containing whitespace must stay one entry.
to_array() {
    local line
    ARRAY_OUT=()
    while IFS= read -r line; do
        if [ -n "$line" ]; then
            ARRAY_OUT+=("$line")
        fi
    done <<< "$1"
    return 0
}
to_array "$CHANGED_ROLES";           CHANGED_ROLES_LIST=(${ARRAY_OUT[@]+"${ARRAY_OUT[@]}"})
to_array "$CHANGED_PLAYBOOKS";       CHANGED_PLAYBOOKS_LIST=(${ARRAY_OUT[@]+"${ARRAY_OUT[@]}"})
to_array "$CHANGED_INVENTORY_PATHS"; CHANGED_INVENTORY_LIST=(${ARRAY_OUT[@]+"${ARRAY_OUT[@]}"})

COVERAGE_SKIPPED=0
if [ -z "$CHANGED_ROLES" ] && [ -z "$CHANGED_PLAYBOOKS" ] && [ -z "$CHANGED_INVENTORY_PATHS" ]; then
    echo "No Ansible role/playbook/inventory changes in this diff; the coverage arm is skipped."
    COVERAGE_SKIPPED=1
fi

# Every path string under `rules: -> changes:` of every deploy job. Custom YAML
# tags (`!reference`) resolve to None so the walker skips that rule entry; the
# referenced job's own `changes:` block is collected independently.
DEPLOY_JOB_PATHS=$(
    python3 - "$CI_FILE" "$JOB_PREFIX" "$JOB_STAGE" <<'PYEOF'
import os
import sys

sys.path.insert(0, os.environ["SCRIPT_DIR"])

try:
    from ci_yaml import NullTagCILoader, jobs, load_ci  # noqa: E402
except ImportError:
    print(
        "ERROR: ci_yaml.py must sit next to this script — vendor both "
        "(see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

ci_path, job_prefix, job_stage = sys.argv[1], sys.argv[2], sys.argv[3]
ci = load_ci(ci_path, loader=NullTagCILoader)

paths = set()
for job_name, job in jobs(ci).items():
    if not job_name.startswith(job_prefix):
        continue
    if job.get("stage") != job_stage:
        # Excludes the coverage-check job itself and any other lint/test job
        # whose name happens to start with the deploy prefix.
        continue
    rules = job.get("rules", [])
    if not isinstance(rules, list):
        continue
    for rule in rules:
        if not isinstance(rule, dict):
            # !reference entries land here (None) — skip cleanly.
            continue
        changes = rule.get("changes", [])
        if isinstance(changes, dict):
            # GitLab also accepts `changes: {paths: [...], compare_to: ...}`.
            changes = changes.get("paths", [])
        if not isinstance(changes, list):
            continue
        for change in changes:
            if isinstance(change, str):
                paths.add((job_name, change))

for job_name, path in sorted(paths):
    print("%s\t%s" % (job_name, path))
PYEOF
)

# The path column alone, for the coverage arms below.
DEPLOY_PATHS=$(printf '%s\n' "$DEPLOY_JOB_PATHS" | cut -f2- | sort -u)

# A literal changes: entry that no longer exists stops triggering its job
# silently, so a rename leaves the deploy path dead with nothing to notice.
STALE_FAILED=0
STALE_ENTRIES=$(
    while IFS=$'\t' read -r job path; do
        [ -z "$path" ] && continue
        # A glob matches a tree, so only a literal entry can be proved dead.
        if [[ "$path" == *'*'* || "$path" == *'?'* ]]; then continue; fi
        if [[ "$path" == *'['* || "$path" == *'{'* ]]; then continue; fi
        [ -e "$path" ] || printf '%s\t%s\n' "$job" "$path"
    done <<< "$DEPLOY_JOB_PATHS"
)

if [ -n "$STALE_ENTRIES" ]; then
    {
        echo "ERROR: The following deploy jobs list a changes: path that does not exist:"
        echo ""
        while IFS=$'\t' read -r job path; do
            [ -z "$path" ] && continue
            echo "  - $job: $path"
        done <<< "$STALE_ENTRIES"
        echo ""
        echo "A literal path left behind by a rename or a deletion matches nothing,"
        echo "so the job stops triggering on the file that replaced it. Point the"
        echo "entry at the real carrier, or drop it from $CI_FILE."
        echo ""
    } >&2
    STALE_FAILED=1
fi

if [ "$COVERAGE_SKIPPED" -eq 1 ]; then
    exit "$STALE_FAILED"
fi

# Any "<roles_dir>/<name>" prefix in a deploy job's changes: list.
MAPPED_ROLES=$(
    printf '%s\n' "$DEPLOY_PATHS" \
        | grep -oE "^${ROLES_ERE}/[A-Za-z0-9_-]+" \
        | sed "s|^${ROLES_DIR}/||" \
        | sort -u \
        || true
)

# Playbooks named verbatim in a deploy job's changes: list. A `**` wildcard gets
# no coverage credit, so it cannot mask a missing trigger.
MAPPED_PLAYBOOKS=$(
    printf '%s\n' "$DEPLOY_PATHS" \
        | grep -oE "^${PLAYBOOKS_ERE}/[A-Za-z0-9_./-]+\.ya?ml$" \
        | sed "s|^${PLAYBOOKS_DIR}/||" \
        | sort -u \
        || true
)

# Inventory paths named verbatim in a deploy job's changes: list.
MAPPED_INVENTORY_PATHS=$(
    printf '%s\n' "$DEPLOY_PATHS" \
        | grep -oE "^${INVENTORY_ERE}/[A-Za-z0-9_./-]+\.ya?ml$" \
        | sed "s|^${INVENTORY_DIR}/||" \
        | sort -u \
        || true
)

# -Fxq: identifiers contain regex metacharacters.
in_list() {
    local needle="$1"
    shift
    [ "$#" -eq 0 ] && return 1
    printf '%s\n' "$@" | grep -Fxq "$needle"
}

UNMAPPED_ROLES=()
for role in ${CHANGED_ROLES_LIST[@]+"${CHANGED_ROLES_LIST[@]}"}; do
    if in_list "$role" ${INTENTIONALLY_UNMAPPED_ROLES[@]+"${INTENTIONALLY_UNMAPPED_ROLES[@]}"}; then
        continue
    fi
    if ! printf '%s\n' "$MAPPED_ROLES" | grep -Fxq "$role"; then
        UNMAPPED_ROLES+=("$role")
    fi
done

UNMAPPED_PLAYBOOKS=()
for pb in ${CHANGED_PLAYBOOKS_LIST[@]+"${CHANGED_PLAYBOOKS_LIST[@]}"}; do
    if in_list "$pb" ${INTENTIONALLY_UNMAPPED_PLAYBOOKS[@]+"${INTENTIONALLY_UNMAPPED_PLAYBOOKS[@]}"}; then
        continue
    fi
    if ! printf '%s\n' "$MAPPED_PLAYBOOKS" | grep -Fxq "$pb"; then
        UNMAPPED_PLAYBOOKS+=("$pb")
    fi
done

UNMAPPED_INVENTORY_PATHS=()
for inv in ${CHANGED_INVENTORY_LIST[@]+"${CHANGED_INVENTORY_LIST[@]}"}; do
    if in_list "$inv" ${INTENTIONALLY_UNMAPPED_INVENTORY_PATHS[@]+"${INTENTIONALLY_UNMAPPED_INVENTORY_PATHS[@]}"}; then
        continue
    fi
    if ! printf '%s\n' "$MAPPED_INVENTORY_PATHS" | grep -Fxq "$inv"; then
        UNMAPPED_INVENTORY_PATHS+=("$inv")
    fi
done

FAILED="$STALE_FAILED"

if [ "${#UNMAPPED_ROLES[@]}" -gt 0 ]; then
    {
        echo "ERROR: The following changed roles are not mapped to any CI deploy job:"
        echo ""
        for role in "${UNMAPPED_ROLES[@]}"; do
            echo "  - $ROLES_DIR/$role/"
        done
        echo ""
        echo "Resolution options:"
        echo "  1. Add the role to the relevant ${JOB_PREFIX}* job's changes: list in"
        echo "     $CI_FILE so the change triggers a rollout. This is the default"
        echo "     expectation for any role that has a CI-driven deploy path."
        echo "  2. Add the role (with a rationale) to the [roles] section of"
        echo "     $CONFIG if it is intentionally deployed manually."
        echo ""
    } >&2
    FAILED=1
fi

if [ "${#UNMAPPED_PLAYBOOKS[@]}" -gt 0 ]; then
    {
        echo "ERROR: The following changed playbooks are not mapped to any CI deploy job:"
        echo ""
        for pb in "${UNMAPPED_PLAYBOOKS[@]}"; do
            echo "  - $PLAYBOOKS_DIR/$pb"
        done
        echo ""
        echo "Resolution options:"
        echo "  1. Add the playbook path to the relevant ${JOB_PREFIX}* job's changes:"
        echo "     list in $CI_FILE so the change triggers a rollout."
        echo "  2. Add the playbook (path relative to $PLAYBOOKS_DIR, with a"
        echo "     rationale) to the [playbooks] section of $CONFIG."
        echo ""
    } >&2
    FAILED=1
fi

if [ "${#UNMAPPED_INVENTORY_PATHS[@]}" -gt 0 ]; then
    {
        echo "ERROR: The following changed inventory paths are not mapped to any CI deploy job:"
        echo ""
        for inv in "${UNMAPPED_INVENTORY_PATHS[@]}"; do
            echo "  - $INVENTORY_DIR/$inv"
        done
        echo ""
        echo "Resolution options:"
        echo "  1. Add the inventory path to the relevant ${JOB_PREFIX}* job's changes:"
        echo "     list in $CI_FILE so the change triggers a rollout."
        echo "  2. Add the path (relative to $INVENTORY_DIR, with a rationale) to"
        echo "     the [inventory] section of $CONFIG."
        echo ""
    } >&2
    FAILED=1
fi

if [ "$FAILED" -eq 1 ]; then
    {
        echo "Either option needs to be in the same MR as the change so the"
        echo "deploy-coverage gate stays accurate."
    } >&2
    exit 1
fi

echo "All changed roles/playbooks/inventory paths are covered by at least one ${JOB_PREFIX}* job rule."
[ -n "$CHANGED_ROLES" ] && echo "Changed roles:           $(echo "$CHANGED_ROLES" | tr '\n' ' ')"
[ -n "$CHANGED_PLAYBOOKS" ] && echo "Changed playbooks:       $(echo "$CHANGED_PLAYBOOKS" | tr '\n' ' ')"
[ -n "$CHANGED_INVENTORY_PATHS" ] && echo "Changed inventory paths: $(echo "$CHANGED_INVENTORY_PATHS" | tr '\n' ' ')"
# The && chains above leave $? = 1 when the last list is empty — don't let
# the success path exit non-zero.
exit 0
