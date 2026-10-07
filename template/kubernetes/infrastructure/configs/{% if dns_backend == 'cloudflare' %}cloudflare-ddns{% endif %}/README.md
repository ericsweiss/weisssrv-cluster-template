# cloudflare-ddns

Keeps the external zone's A records pointed at the site's current public IPv4.
The CronJob in this directory runs `cloudflare-ddns.py` every 5 minutes, mounted
from the `configMapGenerator` in `kustomization.yaml`.

## Environment

| Variable | Meaning |
|---|---|
| `CF_API_TOKEN` | Cloudflare token with DNS edit on the zone, from the Secret. |
| `DDNS_ZONE` | The zone name. |
| `DDNS_RECORDS` | Comma-separated `name[:proxied]` list. `proxied` is spelled `true` or `false` and defaults to `true`. |

`DDNS_RECORDS` is validated strictly. An entry with more than one colon, an
empty name, or a proxied flag that is not `true` or `false` fails the job,
because a malformed flag would otherwise publish the record through the
Cloudflare proxy when a direct record was asked for.

## Record ownership

Terraform and external-dns also create records in this zone. The three owners
must stay on disjoint record names.
