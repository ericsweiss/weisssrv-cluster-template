output "zone_id" {
  description = "Cloudflare zone ID."
  value       = module.zone.zone_id
}

output "name_servers" {
  description = "Nameservers the registrar must delegate to."
  value       = module.zone.name_servers
}

output "record_hostnames" {
  description = "Fully-qualified hostname per record key."
  value       = module.zone.record_hostnames
}
