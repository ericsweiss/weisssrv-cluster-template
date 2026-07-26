terraform {
  # 1.5 floor: the library modules use optional() object defaults and `import`
  # blocks. CI runs hashicorp/terraform:1.15 — state written by a newer minor is
  # unreadable by an older binary, so keep local and CI on the same line.
  required_version = ">= 1.5, < 2.0"

  # GitLab-managed Terraform state, configured entirely through TF_HTTP_* (see
  # the terraform:* tasks and .gitlab-ci.yml).
  backend "http" {}

  required_providers {
    cloudflare = {
      source = "cloudflare/cloudflare"
      # v5 renamed every resource the module uses, so moving to it is a module
      # rewrite plus a state mv per record — never an incidental bump.
      version = "~> 4.52"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
