#!/usr/bin/env bash
# Verify every changed Ansible role, playbook and inventory file matches at
# least one deploy-* job's `changes:` list in the CI file. A failure means an
# Ansible asset was modified but no deploy job will pick it up: either wire the
# path into a deploy-* rule, or list it (with a rationale) in the coverage
# config, which is where every consumer-specific path lives.
#
# Usage:  check-deploy-coverage.sh [BASE_REF]
# Config: $DEPLOY_COVERAGE_CONFIG (default scripts/deploy-coverage.conf);
#         format and defaults in examples/deploy-coverage.example.conf,
#         contract in docs/SCRIPTS.md. Absent config = every list empty.

set -euo pipefail

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

# ERE-safe forms of the configured directories (only '.' needs escaping in the
# path shapes these settings accept).
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

# One diff for every extraction below; `|| true` on each pipe covers the
# "nothing in this category" case. --diff-filter=d drops deletions: a removed
# asset has nothing left to roll out. Renames surface via their added path.
DIFF_FILES=$(git diff --name-only --diff-filter=d "$BASE_REF"...HEAD)

# Extract changed roles (path component after <roles_dir>/).
CHANGED_ROLES=$(
    printf '%s\n' "$DIFF_FILES" \
        | grep -oE "^${ROLES_ERE}/[A-Za-z0-9_-]+" \
        | sed "s|^${ROLES_DIR}/||" \
        | sort -u \
        || true
)

# Extract changed playbooks. Match anything ending in .yml (or .yaml)
# under <playbooks_dir>/ at any depth. Identifier is the path relative to it.
CHANGED_PLAYBOOKS=$(
    printf '%s\n' "$DIFF_FILES" \
        | grep -E "^${PLAYBOOKS_ERE}/.+\.ya?ml$" \
        | sed "s|^${PLAYBOOKS_DIR}/||" \
        | sort -u \
        || true
)

# Extract changed inventory paths under <inventory_dir>/ at any depth. Covers
# group_vars/, host_vars/, the top-level hosts.yml, and any other *.yml/*.yaml
# that may be added (inventory plugin configs, membership files, etc).
CHANGED_INVENTORY_PATHS=$(
    printf '%s\n' "$DIFF_FILES" \
        | grep -E "^${INVENTORY_ERE}/.+\.ya?ml$" \
        | sed "s|^${INVENTORY_DIR}/||" \
        | sort -u \
        || true
)

# Read into arrays line by line: a path containing whitespace must stay one
# entry. (A read loop rather than mapfile, which needs bash 4.)
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

if [ -z "$CHANGED_ROLES" ] && [ -z "$CHANGED_PLAYBOOKS" ] && [ -z "$CHANGED_INVENTORY_PATHS" ]; then
    echo "No Ansible role/playbook/inventory changes in this diff; deploy coverage check skipped."
    exit 0
fi

# Every path string under `rules: -> changes:` of every deploy job. Custom YAML
# tags (`!reference`) resolve to None so the walker skips that rule entry; the
# referenced job's own `changes:` block is collected independently.
DEPLOY_PATHS=$(
    python3 - "$CI_FILE" "$JOB_PREFIX" "$JOB_STAGE" <<'PYEOF'
import sys
import yaml


class _CILoader(yaml.SafeLoader):
    """SafeLoader that tolerates GitLab's custom tags. Subclassed so the
    constructor is not registered on the global SafeLoader."""


# Empty suffix on add_multi_constructor catches every '!<anything>' tag.
_CILoader.add_multi_constructor("!", lambda loader, suffix, node: None)

ci_path, job_prefix, job_stage = sys.argv[1], sys.argv[2], sys.argv[3]
with open(ci_path) as f:
    ci = yaml.load(f, Loader=_CILoader)

paths = set()
for job_name, job in (ci or {}).items():
    if not isinstance(job, dict):
        continue
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
                paths.add(change)

for p in sorted(paths):
    print(p)
PYEOF
)

# Mapped roles: any '<roles_dir>/<name>' prefix inside a deploy job's changes:
# list. Captures both `<roles_dir>/<name>/**` and any literal-file forms.
MAPPED_ROLES=$(
    printf '%s\n' "$DEPLOY_PATHS" \
        | grep -oE "^${ROLES_ERE}/[A-Za-z0-9_-]+" \
        | sed "s|^${ROLES_DIR}/||" \
        | sort -u \
        || true
)

# Mapped playbooks: every '<playbooks_dir>/<path>.yml' that appears verbatim in
# a deploy job's changes: list. Wildcards like `<playbooks_dir>/**` are
# intentionally NOT given coverage credit, so a single ** can't silently mask a
# missing trigger for a newly added playbook.
MAPPED_PLAYBOOKS=$(
    printf '%s\n' "$DEPLOY_PATHS" \
        | grep -oE "^${PLAYBOOKS_ERE}/[A-Za-z0-9_./-]+\.ya?ml$" \
        | sed "s|^${PLAYBOOKS_DIR}/||" \
        | sort -u \
        || true
)

# Mapped inventory paths: explicit '<inventory_dir>/<path>.yml' entries. Same
# wildcard caveat as playbooks; a `group_vars/**` glob does NOT confer coverage
# on every group_var file.
MAPPED_INVENTORY_PATHS=$(
    printf '%s\n' "$DEPLOY_PATHS" \
        | grep -oE "^${INVENTORY_ERE}/[A-Za-z0-9_./-]+\.ya?ml$" \
        | sed "s|^${INVENTORY_DIR}/||" \
        | sort -u \
        || true
)

# Use `grep -Fxq` (fixed-string, whole-line) for membership checks: the
# path/role identifiers contain `.`, `/`, and other regex metachars, so
# a `grep -qx` would silently treat them as regexes and risk false
# matches on e.g. "k3s-srv" vs "k3s.srv".
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

FAILED=0

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
