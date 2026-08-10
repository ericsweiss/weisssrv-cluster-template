#!/usr/bin/env bash
# Adoption / disaster-recovery state bootstrap for terraform/authentik.
#
# Runs `terraform import` for every address in the table below, skipping what is
# already in state, so it is idempotent and safe to re-run. `terraform import`
# only READS the authentik API and writes Terraform state — it never modifies an
# authentik object and never applies config.
#
# Keep the table complete: group names are NOT unique server-side, so an apply
# against a live server with empty state duplicates every group (and hard-fails
# on the applications, whose slugs ARE unique). Re-run this after every apply
# that creates objects and refresh the ids from the module's outputs:
#
#   task terraform:authentik-plan -- -refresh-only   # nothing to apply
#   terraform output application_ids policy_binding_ids group_ids
#
# Invoke via `task terraform:authentik-import` (wraps this in `op run` with the
# TF_VAR_* credentials and the TF_HTTP_* state backend env).
set -euo pipefail
cd "$(dirname "$0")"

# address|id — one line per adopted object.
#   providers    numeric pk        (authentik's API id)
#   applications slug
#   groups       uuid
#   bindings     uuid
# The addresses are module-qualified because the resources live in the library
# module, keyed by their sso.tf map key.
IMPORTS='
module.sso.authentik_provider_oauth2.this["grafana"]|12
module.sso.authentik_group.this["grafana-users"]|00000000-0000-0000-0000-000000000000
module.sso.authentik_application.this["grafana"]|grafana
module.sso.authentik_policy_binding.this["grafana"]|00000000-0000-0000-0000-000000000000
'

STATE="$(terraform state list 2>/dev/null || true)"
imported=0
skipped=0
while IFS='|' read -r addr id; do
  [ -n "${addr}" ] || continue
  case "${addr}" in \#*) continue ;; esac
  if printf '%s\n' "${STATE}" | grep -Fxq "${addr}"; then
    skipped=$((skipped + 1))
    continue
  fi
  echo "==> terraform import '${addr}' '${id}'"
  terraform import -input=false "${addr}" "${id}"
  imported=$((imported + 1))
done <<EOF
${IMPORTS}
EOF

echo "import.sh done: ${imported} imported, ${skipped} already in state."
