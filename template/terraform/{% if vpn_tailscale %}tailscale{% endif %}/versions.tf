terraform {
  required_version = ">= 1.5, < 2.0"

  # Its own state name — never shared with another module (see the terraform:*
  # tasks and .gitlab-ci.yml).
  backend "http" {}

  required_providers {
    tailscale = {
      source = "tailscale/tailscale"
      # Pre-1.0: a minor bump can carry breaking changes, so the minor is pinned
      # and the committed lockfile pins the exact build.
      version = "~> 0.29"
    }
  }
}

provider "tailscale" {
  # The `dns` scope is only needed once split_dns is non-empty, and the OAuth
  # client must be granted it in the admin console too — provider scopes are a
  # subset of what the client holds.
  oauth_client_id     = var.tailscale_oauth_client_id
  oauth_client_secret = var.tailscale_oauth_client_secret
  scopes              = ["acl", "dns"]
}
