# authentik

**The cluster's identity provider.** OIDC and SAML for apps that speak it
(Grafana ships wired to it), plus a Traefik ForwardAuth middleware that puts an
SSO gate in front of apps that speak nothing.

This is the SSO *backend module*. The objects inside it — applications,
providers, groups, policy bindings — are site data and belong in
`terraform/authentik`, not here. This directory only stands the service up.

## What it deploys

- authentik server (2–4 replicas, chart-native HPA) + a single worker
- bundled PostgreSQL on a statically-provisioned local volume
- two IngressRoutes: `auth.<internal>` (LAN/tailnet only) and
  `auth.<external>` (public — an SSO front door has to be reachable from
  wherever your users are)
- two Traefik middlewares (see below)
- a nightly logical `pg_dump` to NFS

`AUTHENTIK_WEB__BASE_URL` pins the canonical origin to `auth.<external_domain>`,
so every OIDC issuer and absolute link authentik builds names the external host
on both routes. The internal route still serves the UI, but its absolute links
point at the external name, so make `auth.<external_domain>` resolve from the
LAN (a split-horizon rewrite, or hairpin NAT) before running the initial-setup
flow.

## The two ForwardAuth middlewares

`authentik-auth` is the one to use. `authentik-auth-basic` is identical **plus**
`Authorization` in `authResponseHeaders`.

That difference matters and is easy to get wrong: Traefik DELETES every header
listed in `authResponseHeaders` from the client request and re-adds it only if
the auth response carries it. Adding `Authorization` to the shared middleware
would therefore strip client-sent credentials (API keys, basic auth) on **every**
route using it. Attach `authentik-auth-basic` only to routes whose upstream
expects authentik-injected basic credentials (a proxy provider with
`basic_auth_enabled`).

## Configure

| Value | Where |
|---|---|
| Chart + image version | `authentik_version` in the versions ConfigMap |
| PostgreSQL image | `postgresql_version` |
| Secrets | item **Authentik Secrets** → `secret-key`, `postgresql-password`, `postgresql-admin-password`; item **SMTP Relay Auth** → `username`, `password` |
| Hostnames | `${cluster_internal_domain}` / `${cluster_external_domain}` |
| Postgres storage | `/mnt/postgres-data` on the node labelled `<node-label-domain>/nas` |
| Dump landing zone | NFS export `/backups-apps/authentik` on `${cluster_nas_host}` |

The pg_dump CronJob runs at 02:30 in `cluster_timezone` and keeps the newest
seven dumps (`RETENTION`). Stagger it against your other dump jobs.

Restore one:

```bash
gunzip -c <file> | psql -h authentik-postgresql -U authentik authentik
```

Nothing to create for the metrics sidecar: the chart's `postgres_exporter`
connects as the `postgres` superuser, which the subchart's own initdb creates
with the `postgresql-admin-password` field the ExternalSecret declares. Rotating
that field needs a matching `ALTER ROLE postgres PASSWORD …`, or the exporter
stops reporting `pg_up 1`. Re-pointing it at the `authentik` role would
duplicate the chart's own `DATA_SOURCE_*` and leave the HelmRelease in
`StateError`.

## Before first login

Nothing can sign in to anything until this is done, and Grafana ships SSO-only,
so do it as soon as the ingress answers:

1. **Bootstrap the built-in admin** at
   `https://auth.<external>/if/flow/initial-setup/`. That flow is available
   only while no admin exists. `group_vars/dns.yml` answers that name with the
   internal VIP on the LAN, so run it from a machine using the new resolvers.
2. **Mint an API token** for `akadmin` (Directory > Tokens) and store it as the
   `Authentik Terraform Token` vault item.
3. **Apply the objects from code.** `terraform/authentik/sso.tf` ships the
   Grafana provider, application, `grafana-users` group and its policy binding;
   `task terraform:authentik-init` / `-plan` / `-apply` creates them. Groups the
   shipped inventory does *not* include — `grafana-admins`, and whatever your
   other apps gate on — belong in `sso.tf` too, not in the UI.

Steps 1 and 2 are UI-only because the module authenticates with a token that
cannot exist before an admin does. Everything after them is code. The bring-up
guide walks the whole sequence, including the Grafana client-id/secret pairing.

## Upgrades

`timeout: 15m` on install and upgrade is deliberate: authentik upgrades run
schema migrations that routinely take 5–10 minutes on modest hardware, and
Flux's 5m default would trigger remediation mid-migration.

## Disable

Remove `- authentik` from `kubernetes/apps/kustomization.yaml`. Then also:

1. drop the `GF_AUTH_GENERIC_OAUTH_*` env, `auth.disable_login_form` and
   `auth.basic.enabled` from
   `infrastructure/observability/kube-prometheus-stack/release.yaml`, or Grafana
   becomes unreachable. Its built-in `admin` account stays — its password is the
   `Grafana SSO` item's `admin-password` field — so that is the way back in;
2. remove `authentik-auth` from every IngressRoute middleware chain that lists
   it, or those routes 500 on every request.

Swapping to another IdP (Keycloak, Zitadel, Dex) means replacing this directory
and the middleware **name** referenced from those chains — the shape (ForwardAuth
+ OIDC) is the same.
