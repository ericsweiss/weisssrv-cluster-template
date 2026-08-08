#!/usr/bin/env bash
# Verify every changed Ansible role, playbook and inventory file matches at
# least one deploy-* job's `changes:` list in .gitlab-ci.yml. Fires in MR CI; a
# failure means an Ansible asset was modified but no deploy job will pick the
# change up — a silent no-op deploy unless the operator either (a) adds the path
# to the relevant deploy-* rule, or (b) acknowledges the asset is intentionally
# outside CI deploy by listing it in the coverage config.
#
# The unmapped gate exists because deploy-trigger mappings can lag the change
# surface: a role can be refactored, a playbook renamed, or an inventory path
# added without anyone updating the deploy-* job's `changes:` list, and the
# rollout would then silently no-op. This forces an explicit acknowledgment in
# the same MR — either wire the asset into a deploy job, or list it (with a
# rationale) in the config.
#
# Usage: check-deploy-coverage.sh [BASE_REF]
#
# CONFIG (default scripts/deploy-coverage.conf, override with
# $DEPLOY_COVERAGE_CONFIG; absent = every list empty):
#
#     [settings]
#     roles_dir = ansible/roles
#     playbooks_dir = ansible/playbooks
#     inventory_dir = ansible/inventories/prod
#     ci_file = .gitlab-ci.yml
#     job_prefix = deploy-
#     job_stage = deploy
#
#     [roles]
#     k3s   # node lifecycle; manual via `task k3s:deploy`
#
#     [playbooks]
#     site.yml   # broad fan-out playbook; each deploy job lists its own triggers
#
#     [inventory]
#     hosts.yml  # affects every deploy; operator picks which jobs to re-run
#
# Every entry MUST carry a trailing `# rationale` and this script ENFORCES that
# (an entry without one is a config error, not a silent exemption) — the
# unmapped lists are only honest if each entry says why it isn't wired to a
# deploy job and which workflow deploys it instead. Prefer wiring the path into
# a deploy-* job's `changes:` list over adding an entry.
#
# Implementation note: deploy-path mappings are extracted by parsing the CI file
# as YAML (python3 + PyYAML) and walking only jobs whose name starts with
# `job_prefix` AND whose `stage:` is `job_stage`. That scope is deliberate: a
# global `grep -oE 'ansible/roles/foo/**'` would match the path inside a
# lint/test job's rules and report "mapped" even though no deploy job will fire —
# false confidence. The walker assumes each deploy job declares its stage
# LITERALLY (a stage inherited only via `extends:` is not resolved, which drops
# that job's paths from coverage credit — a loud false failure on the next
# matching edit, not a silent pass). `changes:` is accepted in both GitLab forms:
# the plain list and the `changes: {paths: [...]}` mapping.

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

# Resolve the diff base in priority order:
#   1. MR pipeline: GitLab provides CI_MERGE_REQUEST_DIFF_BASE_SHA (the
#      target branch tip at MR open).
#   2. Local invocation: positional $1 wins.
#   3. Branch pipeline: CI_COMMIT_BEFORE_SHA = parent of the pushed
#      commit. GitLab sets this to all-zeros on the first push to a
#      brand-new branch — treat that as "fall through" since diff
#      against a null SHA is meaningless.
#   4. Last resort: origin/main. This works locally and in any CI
#      checkout where `origin` is the git remote.
BASE_REF="${CI_MERGE_REQUEST_DIFF_BASE_SHA:-}"
[ -z "$BASE_REF" ] && BASE_REF="${1:-}"
if [ -z "$BASE_REF" ]; then
    if [ -n "${CI_COMMIT_BEFORE_SHA:-}" ] && [ "$CI_COMMIT_BEFORE_SHA" != "0000000000000000000000000000000000000000" ]; then
        BASE_REF="$CI_COMMIT_BEFORE_SHA"
    else
        BASE_REF="origin/main"
    fi
fi

# Hard-fail on a bad ref instead of silently treating "no diff" as
# "everything covered". An invalid BASE_REF used to swallow into
# `git diff … 2>/dev/null` returning empty, which made the script
# exit 0 with "skipped" and gave CI/cron a false-clear signal.
if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
    {
        echo "ERROR: BASE_REF '$BASE_REF' is not a valid git ref or commit."
        echo "       Set CI_MERGE_REQUEST_DIFF_BASE_SHA, pass a valid ref as \$1,"
        echo "       or ensure 'origin/main' is fetched in this checkout."
    } >&2
    exit 2
fi

# Reject unrelated histories. `git diff "$BASE_REF"...HEAD` returns an
# empty diff if BASE_REF and HEAD share no common ancestor (e.g. a
# shallow clone that doesn't reach the MR base, or BASE_REF pointing at
# a sibling repo's tip). Without this guard the script reports "no
# changes — skipped", giving CI a false-clear signal on every commit.
if ! git merge-base --is-ancestor "$BASE_REF" HEAD 2>/dev/null \
   && ! git merge-base "$BASE_REF" HEAD >/dev/null 2>&1; then
    {
        echo "ERROR: BASE_REF '$BASE_REF' shares no common ancestor with HEAD."
        echo "       This is usually a shallow-clone problem in CI (the MR base"
        echo "       commit isn't in the local history). Fetch deeper or unshallow."
    } >&2
    exit 2
fi

# Cache the diff once. The `|| true` at the end of each pipe handles the
# legitimate "nothing changed in this category" case (grep exits 1 on
# no matches, which under `set -e + pipefail` would otherwise abort).
# Real git diff failures already short-circuited at the rev-parse check.
#
# --diff-filter=d EXCLUDES deletions: a removed role/playbook/inventory file
# has no deploy-coverage obligation (there's nothing left to roll out), so it
# must not be flagged as "changed but unmapped" — that would force the operator
# to re-add a just-deleted asset to an intentionally-unmapped list. Renames
# still surface via their added (non-deleted) path.
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

if [ -z "$CHANGED_ROLES" ] && [ -z "$CHANGED_PLAYBOOKS" ] && [ -z "$CHANGED_INVENTORY_PATHS" ]; then
    echo "No Ansible role/playbook/inventory changes in this diff; deploy coverage check skipped."
    exit 0
fi

# Extract every path string under `rules: -> changes:` from every deploy job.
#
# `!reference` and other custom YAML tags appear in rules: lists (e.g.
# `- !reference [deploy-gitlab, rules]`). PyYAML's safe loader would refuse
# those, so we register a multi-constructor that returns a sentinel for any tag —
# the rule entry becomes a non-dict the walker skips, and the referenced job's
# own `changes:` block is still collected independently.
DEPLOY_PATHS=$(
    python3 - "$CI_FILE" "$JOB_PREFIX" "$JOB_STAGE" <<'PYEOF'
import sys
import yaml


def _tag_passthrough(loader, tag_suffix, node):
    return None


# Empty suffix on add_multi_constructor catches every '!<anything>' tag.
yaml.SafeLoader.add_multi_constructor("!", _tag_passthrough)

ci_path, job_prefix, job_stage = sys.argv[1], sys.argv[2], sys.argv[3]
with open(ci_path) as f:
    ci = yaml.safe_load(f)

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
for role in $CHANGED_ROLES; do
    if in_list "$role" ${INTENTIONALLY_UNMAPPED_ROLES[@]+"${INTENTIONALLY_UNMAPPED_ROLES[@]}"}; then
        continue
    fi
    if ! printf '%s\n' "$MAPPED_ROLES" | grep -Fxq "$role"; then
        UNMAPPED_ROLES+=("$role")
    fi
done

UNMAPPED_PLAYBOOKS=()
for pb in $CHANGED_PLAYBOOKS; do
    if in_list "$pb" ${INTENTIONALLY_UNMAPPED_PLAYBOOKS[@]+"${INTENTIONALLY_UNMAPPED_PLAYBOOKS[@]}"}; then
        continue
    fi
    if ! printf '%s\n' "$MAPPED_PLAYBOOKS" | grep -Fxq "$pb"; then
        UNMAPPED_PLAYBOOKS+=("$pb")
    fi
done

UNMAPPED_INVENTORY_PATHS=()
for inv in $CHANGED_INVENTORY_PATHS; do
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
