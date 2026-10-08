# Site data: the apex, the issuance policy and the mail policy. Service names
# belong to external-dns. Each map key is the record's state address and the
# per-record flags pick the resource; see README.md, Record map keys.
locals {
  dns_records = {
    # Protected: deleting it is a full outage. The DDNS CronJob owns the
    # address, so a fresh apply publishes apex_seed_ip (TEST-NET-1) until its
    # next run - seed it yourself: README.md, First apply.
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

    # Losing the CAA set lets any CA issue for the domain, so every entry is
    # protected. The proxied apex also needs Cloudflare's partner-CA entries -
    # drop those only if every record here is DNS-only.
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

    # Mail policy for a domain that sends no mail: hard fail, and protected
    # because silently dropping a `p=reject` record is a security regression.
    # Pointing a real sender at this zone: README.md, Destroy protection.
    spf = {
      name      = "@"
      type      = "TXT"
      content   = "v=spf1 -all"
      comment   = "No host sends mail as this domain"
      protected = true
    }
    # An rua address outside this zone needs <external_domain>._report._dmarc
    # TXT "v=DMARC1" in the RUA domain (RFC 7489) before reporters will send;
    # drop rua if you do not control that domain.
    dmarc = {
      name      = "_dmarc"
      type      = "TXT"
      content   = "v=DMARC1; p=reject; rua=mailto:${var.contact_email}"
      comment   = "Reject anything failing SPF/DKIM alignment; rua best-effort when off-zone"
      protected = true
    }

    # A hostname that must bypass the proxy is an A record with
    # `proxied = false` and `content_managed_externally = true`, port-forwarded
    # on the router. Full attribute set: the module README.
  }
}
