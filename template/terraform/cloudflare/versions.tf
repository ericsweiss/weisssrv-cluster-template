terraform {
  # Floor matches the CI image's Terraform minor (../README.md).
  required_version = ">= 1.15, < 2.0"

  # GitLab-managed Terraform state, configured entirely through TF_HTTP_* (see
  # the terraform:* tasks and .gitlab-ci.yml).
  backend "http" {}

  required_providers {
    cloudflare = {
      source = "cloudflare/cloudflare"
      # Patch line pinned, so a provider bump is its own reviewed change. v5
      # renames every resource the module uses: moving to it is a module rewrite.
      version = "~> 4.52.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
