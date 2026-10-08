#!/usr/bin/env bash
# Lint the alert rules and Alertmanager config that kubeconform cannot reach,
# with promtool / amtool plus the promtool alert unit tests. Run from the repo
# root; contract + env: weisssrv-lib docs/SCRIPTS.md - lint-prometheus-config.
set -euo pipefail

EXTRACT_SCRIPT="${EXTRACT_SCRIPT:-scripts/extract-prometheus-config.py}"
RULE_TESTS_DIR="${RULE_TESTS_DIR:-scripts/prometheus-rule-tests}"

rules_args=()
[ -n "${HELM_RELEASE:-}" ] && rules_args+=(--release "$HELM_RELEASE")
# Whitespace-separated: one --rules-dir per PrometheusRule tree the consumer
# ships, so a per-app tree is linted alongside the shared one.
if [ -n "${RULES_DIR:-}" ]; then
    read -ra rules_dirs <<<"$RULES_DIR"
    for rules_dir in "${rules_dirs[@]}"; do
        rules_args+=(--rules-dir "$rules_dir")
    done
fi
# Set where the HelmRelease is a rule source: a mistyped
# additionalPrometheusRulesMap then reds the gate instead of dropping alerts.
[ -n "${REQUIRE_RELEASE_RULES:-}" ] && rules_args+=(--require-release-rules)
# Set where a PrometheusRule tree is a rule source: an absent tree then reds
# the gate instead of linting a subset.
[ -n "${REQUIRE_RULES_DIR:-}" ] && rules_args+=(--require-rules-dir)
# Extractor flags for a shape neither variable covers.
if [ -n "${EXTRACT_ARGS:-}" ]; then
    read -ra extract_args <<<"$EXTRACT_ARGS"
    rules_args+=("${extract_args[@]}")
fi
am_args=()
[ -n "${AM_CONFIG:-}" ] && am_args=(--am-config "$AM_CONFIG")

for tool in promtool amtool python3; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: $tool not found on PATH" >&2
        exit 1
    }
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "=== Extracting + checking Prometheus alert rules ==="
python3 "$EXTRACT_SCRIPT" rules "$work/rules.yaml" ${rules_args[@]+"${rules_args[@]}"}
promtool check rules "$work/rules.yaml"

echo ""
echo "=== Extracting + checking Alertmanager config ==="
python3 "$EXTRACT_SCRIPT" alertmanager "$work/alertmanager.yaml" ${am_args[@]+"${am_args[@]}"}
amtool check-config "$work/alertmanager.yaml"

echo ""
if ! compgen -G "${RULE_TESTS_DIR}/*.test.yaml" >/dev/null; then
    # Skipping is opt-in: a dropped RULE_TESTS_DIR otherwise greens a gate that
    # ran zero alert unit tests.
    if [ -z "${ALLOW_NO_RULE_TESTS:-}" ]; then
        echo "ERROR: no *.test.yaml in ${RULE_TESTS_DIR}; set RULE_TESTS_DIR to the" >&2
        echo "       unit-test directory, or ALLOW_NO_RULE_TESTS=1 to skip them." >&2
        exit 1
    fi
    echo "No *.test.yaml in ${RULE_TESTS_DIR}; skipping promtool alert unit tests (ALLOW_NO_RULE_TESTS)."
    echo "Prometheus rules + Alertmanager config are valid."
    exit 0
fi

echo "=== Running promtool alert unit tests ==="
# The unit tests run against an annotation-stripped copy, so they assert alert
# logic rather than description prose. `rule_files` in a *.test.yaml resolves
# relative to that file, hence the copy into one directory.
tests_dir="$work/rule-tests"
mkdir -p "$tests_dir"
cp "${RULE_TESTS_DIR}"/*.yaml "$tests_dir"/
python3 - "$work/rules.yaml" "$tests_dir" <<'PY'
import glob
import sys

import yaml


def strip_annotations(path: str, out: str) -> None:
    doc = yaml.safe_load(open(path))
    for group in doc.get("groups", []):
        for rule in group.get("rules", []):
            rule.pop("annotations", None)
    yaml.safe_dump(doc, open(out, "w"), sort_keys=False)


src, out_dir = sys.argv[1], sys.argv[2]
strip_annotations(src, f"{out_dir}/rules.yaml")
for supplementary in glob.glob(f"{out_dir}/*.rules.yaml"):
    strip_annotations(supplementary, supplementary)
PY
promtool test rules "$tests_dir"/*.test.yaml

echo ""
echo "Prometheus rules + Alertmanager config are valid; alert unit tests pass."
