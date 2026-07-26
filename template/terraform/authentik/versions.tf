terraform {
  required_version = ">= 1.5, < 2.0"

  # Its own state name — never shared with another module.
  backend "http" {}

  required_providers {
    authentik = {
      source = "goauthentik/authentik"
      # The provider ships in lockstep with the server: a newer provider can
      # carry schema for API fields an older server does not serve. Pin this to
      # the running server's release line.
      version = ">= 2026.5, < 2027.0"
    }
  }
}

provider "authentik" {
  url   = var.authentik_url
  token = var.authentik_token
}
