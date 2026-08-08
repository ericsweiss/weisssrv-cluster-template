#!/usr/bin/env bash
# Lint the kube-prometheus-stack alert rules + Alertmanager config that
# kubeconform/flux-lint can't reach (PromQL inside HelmRelease values; the
# Alertmanager config inside an ExternalSecret template). Extracts them with
# extract-prometheus-config.py, then validates with promtool / amtool and runs
# the promtool alert unit tests.
#
# Requires promtool + amtool on PATH. Run from the repo root. Non-zero on any
# failure. Environment overrides:
#   EXTRACT_SCRIPT  path to extract-prometheus-config.py
#   RULE_TESTS_DIR  dir of *.test.yaml unit tests (+ any *.rules.yaml they load);
#                   the unit-test step is skipped when it holds no *.test.yaml
#   HELM_RELEASE    HelmRelease manifest holding additionalPrometheusRulesMap
#   AM_CONFIG       ExternalSecret manifest holding the alertmanager.yaml template
set -eo pipefail

EXTRACT_SCRIPT="${EXTRACT_SCRIPT:-scripts/extract-prometheus-config.py}"
RULE_TESTS_DIR="${RULE_TESTS_DIR:-scripts/prometheus-rule-tests}"

rules_args=()
[ -n "${HELM_RELEASE:-}" ] && rules_args=(--release "$HELM_RELEASE")
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
python3 "$EXTRACT_SCRIPT" rules "$work/rules.yaml" "${rules_args[@]}"
promtool check rules "$work/rules.yaml"

echo ""
echo "=== Extracting + checking Alertmanager config ==="
python3 "$EXTRACT_SCRIPT" alertmanager "$work/alertmanager.yaml" "${am_args[@]}"
amtool check-config "$work/alertmanager.yaml"

echo ""
if ! compgen -G "${RULE_TESTS_DIR}/*.test.yaml" >/dev/null; then
    echo "No *.test.yaml in ${RULE_TESTS_DIR}; skipping promtool alert unit tests."
    echo "Prometheus rules + Alertmanager config are valid."
    exit 0
fi

echo "=== Running promtool alert unit tests ==="
# Behavioral tests for the load-bearing alerts (firing/labels/timing). The
# extracted rules keep their annotations for `promtool check rules` above; the
# unit tests run against an annotation-stripped copy so they assert alert logic,
# not churn-prone description prose. rule_files in the *.test.yaml resolve
# relative to the test file's dir, so the tests and any supplementary
# *.rules.yaml are copied alongside the stripped rules.
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
