# Adoption and disaster recovery.
#
# Objects created through the authentik UI are brought under management with
# `import` blocks HERE (the module's resource addresses are stable), then
# `terraform plan` until it is clean. Delete a block once its object is in state
# — a kept block is harmless but noisy.
#
# Group names are NOT unique server-side while application slugs ARE, so an
# apply against a live server with empty state silently duplicates every group
# and hard-fails on the applications. If you rely on re-import for recovery,
# keep this file complete: enumerate the live IDs after every apply that creates
# objects (the module's *_ids outputs give them to you) rather than trusting a
# list written once at adoption time.
#
# import {
#   to = module.sso.authentik_application.this["grafana"]
#   id = "grafana"                       # the slug
# }
#
# import {
#   to = module.sso.authentik_provider_oauth2.this["grafana"]
#   id = "12"                            # the provider pk
# }
#
# import {
#   to = module.sso.authentik_group.this["grafana-users"]
#   id = "018f...-uuid"                  # the group uuid
# }
#
# import {
#   to = module.sso.authentik_policy_binding.this["grafana"]
#   id = "018f...-uuid"                  # the binding uuid
# }
