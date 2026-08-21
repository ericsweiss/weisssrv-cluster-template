terraform {
  # Floor matches the CI image (hashicorp/terraform:1.15), as in the sibling
  # modules — state written by a newer minor is unreadable by an older binary.
  required_version = ">= 1.15, < 2.0"

  # Its own state name — never shared with another module (see the terraform:*
  # tasks and .gitlab-ci.yml).
  backend "http" {}

  required_providers {
    unifi = {
      source = "ubiquiti-community/unifi"
      # Pre-1.0, and a ground-up rewrite of the abandoned paultyng provider:
      # 0.52 -> 0.55 made firewall-policy `index` read-only, added
      # `unifi_network.purpose` and made endpoint match lists Computed. The
      # MINOR is pinned here and the committed lockfile pins the exact build; a
      # minor bump is its own change, with the release notes read first.
      version = "~> 0.55.0"
    }
  }
}

provider "unifi" {
  api_url = var.unifi_api_url
  api_key = var.unifi_api_key
  # The console serves its own self-signed certificate on the LAN address this
  # root talks to, so verification fails on every plan. Nothing here leaves the
  # LAN; a controller reached over the internet would need a real certificate
  # and this line removed.
  allow_insecure = true
}
