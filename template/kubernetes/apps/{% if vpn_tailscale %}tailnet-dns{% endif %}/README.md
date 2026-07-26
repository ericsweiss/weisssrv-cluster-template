# tailnet-dns

**Split-horizon resolver for tailnet clients.** A two-replica CoreDNS exposed to
the tailnet as its own device (`ts-dns`), wired as the tailnet's Split-DNS
nameserver for the internal domain.

**The problem it solves.** A remote client on the tailnet asks for
`app.<internal-domain>`. The LAN resolver answers with the ingress VIP — a LAN
address that only works if the client's traffic is subnet-routed, which is the
slow, fragile path. This resolver instead answers Traefik-fronted names with a
CNAME to the Traefik tailnet device, so the client connects over the mesh
directly. Names that must resolve to a real LAN address (a host, an appliance, a
service that bypasses the ingress) are forwarded to the LAN resolvers unchanged.

**Requires** `vpn_tailscale = true` — the Tailscale operator provides the
`loadBalancerClass: tailscale` this Service uses, and the platform ships a
`traefik-tailnet` Service that is the CNAME target.

## Two values you MUST set before this works

Both live in the `cluster-config` ConfigMap:

| Key | What | How to find it |
|---|---|---|
| `cluster_tailnet_dns_suffix` | Your tailnet's MagicDNS suffix, e.g. `tailXXXXX.ts.net` | `tailscale status` |
| `cluster_upstream_dns_servers` | Space-separated LAN resolver IPs the override zone forwards to | your DNS hosts |

If either is empty the rendered Corefile is invalid or forwards nowhere, and the
Deployment crash-loops on config parse. There is no safe default for either.

The CNAME target is `traefik-tailnet.<suffix>` — deterministic from the Traefik
tailnet Service's `tailscale.com/hostname`, so there is no bootstrap
circularity.

## The override list

`configmap.yaml` has one zone listing names that must NOT be CNAME'd to Traefik.
Ship it with the direct-IP names your DNS layer defines (hosts, appliances,
anything bypassing the ingress) and **keep it in sync** with the DNS role's
rewrite list — a name added there and forgotten here resolves to Traefik and
breaks. Every other name under the internal domain is assumed Traefik-fronted
and needs no entry.

## Wiring

After the Service gets its tailnet address, set it as the Split-DNS nameserver
for the internal domain in your tailnet policy (the Terraform tailscale module
reads the address back and does this for you).

## Disable

Remove the entry from `kubernetes/apps/kustomization.yaml` and drop the Split-DNS
nameserver from the tailnet policy. Remote clients fall back to whatever the
tailnet's default resolver answers.
