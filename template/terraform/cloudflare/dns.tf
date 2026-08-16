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
    #
    # A fresh apply publishes apex_seed_ip (TEST-NET-1) until the CronJob's next
    # run, so the site is dark with a clean plan. Trigger the first run yourself:
    #
    #   kubectl -n cloudflare-ddns create job --from=cronjob/cloudflare-ddns ddns-seed
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
    # domain, so every entry is protected. Add a tag pair per CA you use.
    #
    # The apex is proxied, so Cloudflare's edge certificate comes from one of its
    # Universal SSL partner CAs, not the CA the cluster uses — both sets need
    # entries or edge renewal fails. Drop the partner entries only if every
    # record here is DNS-only.
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
    caa_issue_pki_goog = {
      name        = "@"
      type        = "CAA"
      record_data = { flags = 0, tag = "issue", value = "pki.goog" }
      comment     = "Cloudflare Universal SSL partner CA (Google Trust Services)"
      protected   = true
    }
    caa_issuewild_pki_goog = {
      name        = "@"
      type        = "CAA"
      record_data = { flags = 0, tag = "issuewild", value = "pki.goog" }
      comment     = "Cloudflare Universal SSL partner CA wildcard (Google Trust Services)"
      protected   = true
    }
    caa_issue_ssl_com = {
      name        = "@"
      type        = "CAA"
      record_data = { flags = 0, tag = "issue", value = "ssl.com" }
      comment     = "Cloudflare Universal SSL partner CA (SSL.com)"
      protected   = true
    }
    caa_issuewild_ssl_com = {
      name        = "@"
      type        = "CAA"
      record_data = { flags = 0, tag = "issuewild", value = "ssl.com" }
      comment     = "Cloudflare Universal SSL partner CA wildcard (SSL.com)"
      protected   = true
    }
    caa_iodef = {
      name        = "@"
      type        = "CAA"
      record_data = { flags = 0, tag = "iodef", value = "mailto:${var.contact_email}" }
      comment     = "Where CAs report issuance-policy violations"
      protected   = true
    }

    # Mail policy for a domain that sends no mail: hard fail, since nothing
    # legitimate can be rejected. Protected because silently dropping a
    # `p=reject` DMARC record is a security regression a plan should refuse.
    #
    # The SMTP relay this repo deploys sends as the INTERNAL domain, so nothing
    # here is affected. Pointing a sender at THIS zone means relaxing both
    # records and adding DKIM first, then tightening back once alignment is
    # clean.
    spf = {
      name      = "@"
      type      = "TXT"
      content   = "v=spf1 -all"
      comment   = "No host sends mail as this domain"
      protected = true
    }
    dmarc = {
      name      = "_dmarc"
      type      = "TXT"
      content   = "v=DMARC1; p=reject; rua=mailto:${var.contact_email}"
      comment   = "Reject anything failing SPF/DKIM alignment"
      protected = true
    }

    # A hostname that must bypass the proxy (large uploads, UDP, a non-HTTP
    # port) is an A record with `proxied = false` and
    # `content_managed_externally = true`, port-forwarded on the router. The
    # module README has the full attribute set.
  }
}
