terraform {
  # Floor matches the CI image (hashicorp/terraform:1.15), as in the sibling
  # modules — state written by a newer minor is unreadable by an older binary.
  required_version = ">= 1.15, < 2.0"

  # Its own state name — never shared with another module.
  backend "http" {}

  required_providers {
    authentik = {
      source = "goauthentik/authentik"
      # EXACT pin, in lockstep with the server: a newer provider can carry
      # schema for API fields an older server does not serve. The minor must
      # match `authentik_version` in group_vars/all.yml, and a bump rides the
      # server upgrade. The library module declares a release-line range so a
      # caller can narrow it; narrowing it is this line's job.
      version = "2026.5.0"
    }
  }
}

provider "authentik" {
  url   = var.authentik_url
  token = var.authentik_token
}
