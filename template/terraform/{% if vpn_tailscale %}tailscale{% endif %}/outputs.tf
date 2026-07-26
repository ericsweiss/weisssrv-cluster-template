output "acl_id" {
  description = "ID of the managed tailnet ACL resource."
  value       = module.tailnet.acl_id
}

output "split_dns_nameservers" {
  description = "Resolved nameserver IPs per Split-DNS domain."
  value       = module.tailnet.split_dns_nameservers
}
