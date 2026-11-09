terraform {
  # The floor admits the CI image and the workstation toolchain. State a newer
  # minor writes may not load in an older binary, so local runs track the CI
  # minor. Every root shares the floor.
  required_version = ">= 1.15, < 2.0"

  # Its own state name — never shared with another module (see the terraform:*
  # tasks and .gitlab-ci.yml).
  backend "http" {}

  required_providers {
    unifi = {
      source = "ubiquiti-community/unifi"
      # Pre-1.0, pinned to the patch line (../README.md).
      version = "~> 0.55.0"
    }
  }
}

provider "unifi" {
  api_url = var.unifi_api_url
  api_key = var.unifi_api_key
  # Defaults to true: the console serves a self-signed certificate on the LAN
  # address this root talks to. A console with a trusted certificate sets
  # TF_VAR_unifi_allow_insecure=false, no source edit (see variables.tf).
  allow_insecure = var.unifi_allow_insecure
}
