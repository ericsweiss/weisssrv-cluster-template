# cloudflare-ddns

Keeps the external zone's A records pointed at the site's current public IPv4.
The CronJob in this directory runs `cloudflare-ddns.py` every 5 minutes, mounted
from the `configMapGenerator` in `kustomization.yaml`. Its site wiring is tested
by `tests/test_cloudflare_ddns.py`.

## Environment

The program reads no site data of its own. Every value arrives as an env var the
CronJob spells as a `cluster-config` placeholder.

| Variable | Source | Meaning |
|---|---|---|
| `CF_API_TOKEN` | `dns-api-token` Secret | Token with DNS edit on the zone. |
| `DDNS_ZONE` | `${cluster_external_domain}` | The zone name. |
| `DDNS_RECORDS` | `${cluster_ddns_records}` | Comma-separated `label[:proxied]` list. `@` is the apex. `proxied` is `true` or `false` and defaults to `true`. A label is never a full name. |

The program is vendored byte-identical from weisssrv-lib, whose `docs/SCRIPTS.md`
owns its contract: input validation, the two-provider address check and the exit
codes. A run that fails on any record exits non-zero.

`proxied` seeds creation only. On update the record's live value wins, so this
job and Terraform cannot flip-flop it on every cycle.

## Record ownership

Terraform and external-dns also create records in this zone. The three owners
must stay on disjoint record names.
