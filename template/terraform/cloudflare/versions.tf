terraform {
  # Floor matches the CI image (hashicorp/terraform:1.15): state written by a
  # newer minor is unreadable by an older binary, so local and CI stay on the
  # same line. The library modules only need 1.5 (optional() object defaults,
  # `import` blocks); this is the stricter of the two.
  required_version = ">= 1.15, < 2.0"

  # GitLab-managed Terraform state, configured entirely through TF_HTTP_* (see
  # the terraform:* tasks and .gitlab-ci.yml).
  backend "http" {}

  required_providers {
    cloudflare = {
      source = "cloudflare/cloudflare"
      # Patch line pinned: v4 minors have shipped schema and deprecation
      # changes, so a minor bump is a deliberate edit here. v5 renamed every
      # resource the module uses, so moving to it is a module rewrite plus a
      # state mv per record — never an incidental bump.
      version = "~> 4.52.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
