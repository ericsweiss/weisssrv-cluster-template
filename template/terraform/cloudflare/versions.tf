terraform {
  # Floor matches the CI image's Terraform minor (../README.md).
  required_version = ">= 1.15, < 2.0"

  # GitLab-managed Terraform state, configured entirely through TF_HTTP_* (see
  # the terraform:* tasks and .gitlab-ci.yml).
  backend "http" {}

  required_providers {
    cloudflare = {
      source = "cloudflare/cloudflare"
      # Patch line pinned (../README.md). v5 renames every resource the module
      # uses: moving to it is a module rewrite plus a state mv per record.
      version = "~> 4.52.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
