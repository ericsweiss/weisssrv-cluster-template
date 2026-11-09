output "application_ids" {
  description = "Application ID per slug."
  value       = module.sso.application_ids
}

output "oauth2_client_ids" {
  description = "Client ID per OAuth2 provider key — what each application's OIDC config must use."
  value       = module.sso.oauth2_client_ids
}

output "oauth2_provider_ids" {
  description = "OAuth2 provider pk per key. Record these: a disaster-recovery re-import needs them."
  value       = module.sso.oauth2_provider_ids
}

output "proxy_provider_ids" {
  description = "Proxy provider pk per key. Record these: a disaster-recovery re-import needs them."
  value       = module.sso.proxy_provider_ids
}

output "saml_provider_ids" {
  description = "SAML provider pk per key. Record these: a disaster-recovery re-import needs them."
  value       = module.sso.saml_provider_ids
}

output "custom_scope_mapping_ids" {
  description = "Scope-mapping ID per custom_scope_mappings key. Record these: a disaster-recovery re-import needs them."
  value       = module.sso.custom_scope_mapping_ids
}

output "policy_binding_ids" {
  description = "Binding UUID per key. Record these: a disaster-recovery re-import needs them."
  value       = module.sso.policy_binding_ids
}

output "group_ids" {
  description = "Group ID per groups key."
  value       = module.sso.group_ids
}
