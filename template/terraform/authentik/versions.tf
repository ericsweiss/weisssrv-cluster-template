terraform {
  # Floor matches the CI image's Terraform minor (../README.md).
  required_version = ">= 1.15, < 2.0"

  # Its own state name — never shared with another module.
  backend "http" {}

  required_providers {
    authentik = {
      source = "goauthentik/authentik"
      # EXACT pin, minor-locked to `authentik_version` in group_vars/all.yml —
      # a provider newer than the server carries schema the API does not serve.
      version = "2026.8.0"
    }
  }
}

provider "authentik" {
  url   = var.authentik_url
  token = var.authentik_token
}
