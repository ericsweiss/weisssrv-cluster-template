output "network_ids" {
  description = "Network id per `networks` key. Record these: an import of a client, a WLAN or a zone is checked against them."
  value       = module.network.network_ids
}

output "zone_ids" {
  description = "Firewall-zone id per zone key — the custom `zones` keys and the built-in short names in one map, which is the namespace the policies resolve against."
  value       = module.network.zone_ids
}

output "wlan_ids" {
  description = "WLAN id per `wlans` key."
  value       = module.network.wlan_ids
}
