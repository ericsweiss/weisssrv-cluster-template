# registry-cache

In-cluster **pull-through container registry cache** (CNCF `distribution`).

**What it does.** Proxies one upstream registry. The first pull of an image
fetches from upstream and caches the blobs; every later pull is served
node-locally. Written for CI: a fresh DinD daemon per job otherwise cold-pulls
the same job image dozens of times per pipeline.

**When you want it.** You run in-cluster CI, or any workload that repeatedly
pulls the same large images. If neither is true, disable it.

**When you do NOT want it.** If your upstream is a public registry with its own
CDN and your image churn is low, this is extra moving parts for no gain.

## Shape

One Deployment, one container in proxy mode, one ClusterIP:

- `:5000` — registry API. Reachable only from the CI runner namespace.
- `:5001` — debug listener serving `/metrics`. Reachable only from the
  Prometheus pod (the debug listener also serves `/debug` endpoints, so
  admitting the whole observability namespace would over-share).

Storage is a node-local `emptyDir` with a hard cap: the cache re-warms after any
restart, so there is nothing to back up and nothing to mount. Exceeding the cap
evicts the pod, which reschedules and re-warms — bounded by design.

## Configure

| Value | Where |
|---|---|
| Upstream registry URL | `deployment.yaml` — `registry.${cluster_git_host}` by default (the self-hosted registry convention). Edit that one env var for any other upstream. |
| Upstream credentials | secrets item **Registry Cache Upstream** → fields `username`, `password` |
| Image version | `registry_cache_version` in the versions ConfigMap |
| Consumer namespace | `networkpolicy.yaml`, `allow-registry-cache-ingress` |

Give the upstream credentials the narrowest read-only scope the registry offers
(a GitLab deploy token with `read_registry`, a GHCR read:packages PAT, …). The
cache only ever pulls.

### Upstream on the public internet

The default egress policy allows DNS plus `:443` **to the cluster's own Traefik
namespace** — i.e. it assumes the upstream registry is fronted by this cluster's
internal ingress. For a public upstream (Docker Hub, GHCR, quay.io) replace that
rule with the shared `netpol-egress-public` component in `kustomization.yaml`.

### Name resolution for a self-hosted upstream

If the upstream hostname resolves publicly but is actually served by this
cluster's internal VIP, pods will hairpin over the WAN. `deployment.yaml` carries
a commented `hostAliases` block that pins the name to
`${cluster_metallb_internal_vip}` at pod scope — uncomment it in that case. TLS
is unaffected: SNI and cert validation still use the hostname.

## Disable

Remove `- registry-cache` from `kubernetes/apps/kustomization.yaml` and push.
Flux prunes the namespace and everything in it. Nothing else depends on it — a
cache outage is slower pulls, not an outage, which is why its alert (if you add
one) should be `warning`.
