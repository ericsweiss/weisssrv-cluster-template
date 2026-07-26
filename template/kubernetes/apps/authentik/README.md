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

The pg_dump CronJob runs at 02:30 **UTC**. Add `spec.timeZone` if you need it
aligned with local overnight maintenance windows.

## Before first login

Create the `grafana-users` / `grafana-admins` groups (and whatever groups your
other apps gate on) in authentik, and bootstrap the initial admin via the
chart's `/if/flow/initial-setup/` flow. The Terraform module then imports and
owns the object inventory from there.

## Upgrades

`timeout: 15m` on install and upgrade is deliberate: authentik upgrades run
schema migrations that routinely take 5–10 minutes on modest hardware, and
Flux's 5m default would trigger remediation mid-migration.

## Disable

Remove `- authentik` from `kubernetes/apps/kustomization.yaml`. Then also:

1. drop the `GF_AUTH_GENERIC_OAUTH_*` env and `disable_login_form` from
   `infrastructure/observability/kube-prometheus-stack/release.yaml`, or Grafana
   becomes unreachable;
2. remove `authentik-auth` from every IngressRoute middleware chain that lists
   it, or those routes 500 on every request.

Swapping to another IdP (Keycloak, Zitadel, Dex) means replacing this directory
and the middleware **name** referenced from those chains — the shape (ForwardAuth
+ OIDC) is the same.
