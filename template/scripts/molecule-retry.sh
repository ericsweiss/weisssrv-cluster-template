#!/usr/bin/env bash
# Run `molecule test` with an in-job destroy + jittered retry. Only the setup
# stages retry: a failure at or after converge is the scenario's verdict.
# Contract + env: weisssrv-lib docs/SCRIPTS.md - molecule-retry.sh.
set -uo pipefail

MOL_MAX="${MOL_MAX:-4}"
MOL_BASE="${MOL_BASE:-}"
MOL_SCEN="${MOL_SCEN:-}"

# Stage banners molecule prints when it enters a test stage. Reaching one means
# the scenario ran, so a retry would let a flaky assertion pass green.
TEST_STAGES='converge|idempotence|side_effect|verify'
BANNER_RE="(Running .* > (${TEST_STAGES})\b)|(Action: '(${TEST_STAGES})')"
# Stage-agnostic form: no banner of any shape means molecule's output format
# changed, and BANNER_RE can no longer tell a setup failure from a verdict.
ANY_BANNER_RE="(Running .* > [a-z_]+)|(Action: '[a-z_]+')"

# Attempt count for the job's dotenv report; unset means no report.
record_attempts() {
    echo "molecule-retry: finished after $1 attempt(s)"
    [ -n "${MOLECULE_RETRY_DOTENV:-}" ] || return 0
    echo "MOLECULE_RETRY_ATTEMPTS=$1" >"$MOLECULE_RETRY_DOTENV"
}

# Move an attempt's junit out of the way so only the deciding attempt's XMLs
# reach the pipeline test report, while the failed ones stay downloadable.
stash_junit() {
    [ -n "${JUNIT_OUTPUT_DIR:-}" ] && [ -d "$JUNIT_OUTPUT_DIR" ] || return 0
    local failed_dir="$JUNIT_OUTPUT_DIR/failed-attempt-$1"
    mkdir -p "$failed_dir"
    find "$JUNIT_OUTPUT_DIR" -maxdepth 1 -name '*.xml' -exec mv {} "$failed_dir"/ \;
}

log=$(mktemp)
trap 'rm -f "$log"' EXIT

attempt=1
while true; do
    # shellcheck disable=SC2086  # intentional word-split of MOL_BASE/MOL_SCEN
    molecule $MOL_BASE test $MOL_SCEN 2>&1 | tee "$log"
    rc="${PIPESTATUS[0]}"
    if [ "$rc" -eq 0 ]; then
        record_attempts "$attempt"
        exit 0
    fi

    if grep -Eq "$BANNER_RE" "$log"; then
        echo "molecule-retry: failure at or after converge; not retrying (rc=$rc)"
        record_attempts "$attempt"
        exit "$rc"
    fi

    if ! grep -Eq "$ANY_BANNER_RE" "$log"; then
        echo "molecule-retry: no stage banner recognised - molecule output format changed; refusing to retry (rc=$rc)"
        record_attempts "$attempt"
        exit "$rc"
    fi

    if [ "$attempt" -ge "$MOL_MAX" ]; then
        record_attempts "$attempt"
        exit "$rc"
    fi

    stash_junit "$attempt"
    attempt=$((attempt + 1))
    echo "molecule-retry: setup stage failed (rc=$rc); destroying + retrying ($attempt/$MOL_MAX)"
    # shellcheck disable=SC2086  # intentional word-split of MOL_BASE/MOL_SCEN
    molecule $MOL_BASE destroy $MOL_SCEN || true
    # 20-65s jitter so simultaneous retries across the fan-out do not re-collide.
    sleep $(((RANDOM % 46) + 20))
done
