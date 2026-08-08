# vm-ingress

**The pattern for putting cluster ingress in front of something that is not in
the cluster** — a VM, an LXC container, an appliance, the router's web UI.

This directory ships the *mechanism* plus one worked example. The route inventory
is site data: add one file per backend.

## Why this shape

Traefik routes to Kubernetes Services. An off-cluster host has no Service and no
Endpoints controller to build one, so you create both by hand:

1. a **ClusterIP Service with no selector** — nothing will ever populate it
   automatically;
2. a **manually managed EndpointSlice** carrying the backend's IP and port,
   labelled `kubernetes.io/service-name: <service>` and
   `endpointslice.kubernetes.io/managed-by: manual`;
3. an **IngressRoute** matching the hostname and pointing at that Service.

The payoff is that every off-cluster service gets the same TLS termination,
middleware chain, access control and certificate lifecycle as an in-cluster app —
one ingress story, not two.

## HTTPS backends and the shared ServersTransport

`serverstransport.yaml` defines `vm-tls-wildcard`, used by any route whose
backend speaks HTTPS.

The problem it solves: Traefik connects to the backend by **IP**, so SNI does not
naturally match the certificate the backend presents. Without help you would have
to set `insecureSkipVerify` and give up backend verification entirely. Setting
`serverName` explicitly to a name covered by the wildcard SAN makes validation
succeed against the real certificate. The name is a sentinel — it is never
resolved.

This assumes each backend serves a copy of the internal wildcard certificate (the
`acme_certs` role distributes it after every renewal).

**ServersTransport references resolve in the IngressRoute's OWN namespace**
(Traefik's CRD provider is namespace-scoped). If you put routes in another
namespace, copy the ServersTransport there too.

Backend TLS *version* policy is the backend's responsibility — the
ServersTransport CRD exposes no `minVersion` for backend connections.

## Adding a route

Copy `example-route.yaml`, edit the four things that vary (name, backend IP,
port, hostname), and add the file to `kustomization.yaml`. The example is
deliberately **not** listed there: it points at a placeholder address, and an
unreachable route reconciled into the cluster is just a permanently failing
health check.

Choose the exposure per route:

| Exposure | Middlewares | TLS secret |
|---|---|---|
| Internal only | `lan-tailscale-only` + `hsts-header` | internal wildcard |
| Public | `hsts-header` (+ `external-dns` target annotation) | external wildcard |
| Public, SSO-gated | `authentik-auth` + `hsts-header` | external wildcard |

Anything reachable from the internet that is not itself an identity provider
should carry an auth middleware.

## Gotchas worth knowing before you debug them

- **A plain HTTP backend needs no ServersTransport** — drop `scheme: https` and
  the `serversTransport` line.
- **Websocket backends** (home automation, consoles) work through Traefik
  unchanged, but some need `X-Forwarded-Proto: https` injected; add a small
  `headers` Middleware for those.
- **The EndpointSlice is not health-checked.** If the backend is down, requests
  fail at connect. Add a blackbox probe for anything you care about.
- **A backend that is legitimately powered off much of the time** (an on-demand
  desktop) will fire `EndpointDown` forever. Give it a purpose-built alert gated
  on the guest's power state instead of relying on the generic probe rule.
