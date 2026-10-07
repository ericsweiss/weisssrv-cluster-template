# Dashboards

The platform dashboard set. Each JSON file is a `configMapGenerator` entry in
`kustomization.yaml`; the Grafana sidecar picks the ConfigMaps up by their
`grafana_dashboard: "1"` label.

## Provenance

`source` is the grafana.com dashboard id for an import, or `hand-written`.
`schema` / `version` are the values inside the file, so a re-import that changes
them is visible in the diff.

| File | Source | schema / version | Local changes |
|---|---|---|---|
| `alerts-overview.json` | hand-written | 39 / 1 | — |
| `alertmanager-self.json` | grafana.com 9578 (rev 4) | 39 / 28 | uid `alertmanager-self`; datasource refs pinned to uid `prometheus`; materialized repeat clones removed; every selector scoped by the `$pod` variable; memory limit overlay added; a collapsed `Cluster (gossip)` row added; timezone `browser` |
| `blackbox.json` | hand-written | 39 / 1 | — |
| `cert-manager.json` | hand-written | 39 / 1 | — |
| `cluster-overview.json` | hand-written | 39 / 1 | — |
| `flux-cluster.json` | hand-written | 39 / 1 | — |
| `loki-self.json` | hand-written | 39 / 1 | — |
| `node-exporter-full.json` | grafana.com 1860 (rev 45) | 41 / 101 | uid `node-exporter-full`; tags infrastructure/node/linux; time `now-6h`; `__inputs`/`__requires` stripped and datasource refs pinned to uid `prometheus`; panels IRQ Detail, TCP Stat Persistent, TCP Stat Transient and TCP Socket Queue removed, and the `node_pressure_irq_stalled_seconds_total` / `node_netstat_Tcp_MaxConn` targets dropped, because those collectors are off; Entropy moved to x=0 to close the gap IRQ Detail left |
| `prometheus-self.json` | grafana.com 19105 (rev 9) | 38 / 9 | uid `prometheus-self`; title `Prometheus`; `__inputs`/`__elements`/`__requires` stripped; the exported `datasource` variable dropped and every ref pinned to uid `prometheus`; timezone set to `browser` |
| `traefik-official.json` | grafana.com 17347 | 37 / 7 | datasource refs pinned to uid `prometheus` |

A re-import downloads
`https://grafana.com/api/dashboards/<id>/revisions/<rev>/download`, re-applies
the Local changes column, and bumps the recorded revision. Diff the current file
against the revision you are moving from, so those changes are carried forward
rather than rediscovered.

## What a new dashboard must satisfy

- Every datasource ref is pinned to uid `prometheus` (or `loki`). The chart
  fixes those uids, so a dashboard that carries an exported uid loads broken.
- The `uid` is stable and unique, and never changes after the first import. The
  sidecar keys the ConfigMap to the dashboard on it, which is also why
  `kustomization.yaml` sets `disableNameSuffixHash: true`. The uid need not
  match the file name: three of these end in `-platform`.
- `"timezone": "browser"`, so every dashboard in the set renders timestamps the
  same way.
- A `"refresh"` interval is set, `"1m"` unless the panels are costly. Without it
  a dashboard left open on a wall display never updates.
