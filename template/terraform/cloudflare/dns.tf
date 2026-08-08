# Site data: the records Terraform owns in the external zone.
#
# Most service names are deliberately absent — external-dns creates them
# in-cluster from each IngressRoute, and a Terraform copy would fight it. What
# lives here is what nothing else can create: the apex, the issuance policy, and
# the mail policy.
#
# Each map key is the resource's STATE ADDRESS. Renaming a key destroys and
# recreates that record unless you add a `moved {}` block. Two per-record flags
# select the lifecycle (module README has the full table):
#
#   protected                  = true  ->  lifecycle.prevent_destroy
#   content_managed_externally = true  ->  Terraform stops diffing `content`
#
# Flipping either flag changes the resource address, so it plans a destroy and
# create — the same `moved {}` rule applies.
locals {
  dns_records = {
    # The public entry point. Protected because deleting it is a full outage,
    # and external-content because the DDNS CronJob owns the address.
    apex = {
      name                       = var.external_domain
      type                       = "A"
      content                    = var.apex_seed_ip
      proxied                    = true
      ttl                        = 1 # required to be 1 ("Auto") while proxied
      comment                    = "Seeded by Terraform; address owned by the DDNS job"
      protected                  = true
      content_managed_externally = true
    }

    # Certificate issuance policy. Losing the CAA set lets ANY CA issue for the
    # domain, so every entry is protected. Add a tag pair per CA you use — if
    # the zone is behind a proxy whose edge certificates come from the provider's
    # partner CAs, those CAs need entries too or edge renewal breaks.
    caa_issue_letsencrypt = {
      name        = "@"
      type        = "CAA"
      record_data = { flags = 0, tag = "issue", value = "letsencrypt.org" }
      comment     = "Restrict issuance to Let's Encrypt"
      protected   = true
    }
    caa_issuewild_letsencrypt = {
      name        = "@"
      type        = "CAA"
      record_data = { flags = 0, tag = "issuewild", value = "letsencrypt.org" }
      comment     = "Restrict wildcard issuance to Let's Encrypt"
      protected   = true
    }
    caa_iodef = {
      name        = "@"
      type        = "CAA"
      record_data = { flags = 0, tag = "iodef", value = "mailto:${var.contact_email}" }
      comment     = "Where CAs report issuance-policy violations"
      protected   = true
    }

    # Mail policy for a domain that sends no mail. Replace both if you add a
    # sender, and add the DKIM record its provider gives you.
    spf = {
      name    = "@"
      type    = "TXT"
      content = "v=spf1 -all"
      comment = "No host sends mail as this domain"
    }
    dmarc = {
      name    = "_dmarc"
      type    = "TXT"
      content = "v=DMARC1; p=reject; rua=mailto:${var.contact_email}"
      comment = "Reject anything failing SPF/DKIM alignment"
    }

    # A hostname that must bypass the proxy — large uploads and non-HTTP
    # protocols break behind it. Point it at your WAN address and port-forward.
    #
    # upload = {
    #   name                       = "upload"
    #   type                       = "A"
    #   content                    = var.apex_seed_ip
    #   proxied                    = false
    #   comment                    = "DNS-only: proxy caps request bodies"
    #   content_managed_externally = true
    # }
  }
}
