#!/usr/bin/env bash
# Run `molecule test` with an in-job destroy + jitter retry, shared by the
# molecule-tests and integration-tests CI jobs so the retry policy (max attempts
# + jitter window) lives in ONE place instead of two hand-synced copies.
#
# Concurrent systemd-container starts race cgroup setup and die at prepare;
# destroy + a jittered sleep de-conflicts the retry, where a job-level retry
# would land every attempt in the same storm.
#
# The caller cd's into the role / integration-test directory first; molecule
# runs in $PWD. Base opts (before the subcommand, e.g. `-c <base.yml>`) and
# scenario opts (after it, e.g. `-s default`) are passed via env so both apply
# to the `test` AND the `destroy` invocations:
#   MOL_MAX  = max attempts            (default 4)
#   MOL_BASE = args before subcommand  (default empty)
#   MOL_SCEN = args after subcommand   (default empty)
#
# Usage:
#   MOL_BASE="-c $CI_PROJECT_DIR/ansible/molecule/base.yml" MOL_SCEN="-s default" \
#     bash "$CI_PROJECT_DIR/scripts/molecule-retry.sh"
#   bash "$CI_PROJECT_DIR/scripts/molecule-retry.sh"   # integration-tests: no base/scen
set -uo pipefail

MOL_MAX="${MOL_MAX:-4}"
MOL_BASE="${MOL_BASE:-}"
MOL_SCEN="${MOL_SCEN:-}"

attempt=1
# shellcheck disable=SC2086  # intentional word-split of MOL_BASE/MOL_SCEN
until molecule $MOL_BASE test $MOL_SCEN; do
    rc=$?
    attempt=$((attempt + 1))
    if [ "$attempt" -gt "$MOL_MAX" ]; then
        exit "$rc"
    fi
    echo "molecule attempt $((attempt - 1)) failed (rc=$rc); destroying + retrying ($attempt/$MOL_MAX)"
    # Clear the failed attempt's junit XMLs: the callback appends one file per
    # playbook run, so without this a transient attempt-1 failure uploads its
    # red testcases ALONGSIDE the passing retry's — the pipeline test report
    # must reflect only the attempt that determined job status.
    if [ -n "${JUNIT_OUTPUT_DIR:-}" ] && [ -d "$JUNIT_OUTPUT_DIR" ]; then
        rm -f "$JUNIT_OUTPUT_DIR"/*.xml
    fi
    # shellcheck disable=SC2086  # intentional word-split of MOL_BASE/MOL_SCEN
    molecule $MOL_BASE destroy $MOL_SCEN || true
    # 20-65s jitter so simultaneous retries across the fan-out do not re-collide.
    sleep $(((RANDOM % 46) + 20))
done
