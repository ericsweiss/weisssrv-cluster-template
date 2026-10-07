# Debugging: symptom to entry point

`docs/RUNBOOKS.md` § Where to look first is canonical. This page is the index
from a symptom to the first command.

| Symptom | Start here |
|---|---|
| A manifest change did not take effect | `task flux:status`, then `task flux:reconcile`. A stalled Kustomization names the object it could not apply |
| `${...}` appears in a live object | the Kustomization is missing a `substituteFrom` entry; every stage after `sources` reads BOTH the versions and the cluster-config ConfigMaps |
| A HelmRelease will not upgrade | its events, then `task flux:lint` locally — a values schema error fails the same way in both places |
| A pod cannot reach a dependency | its namespace's NetworkPolicies. An ingress default-deny is present everywhere, so a new dependency needs a new allow |
| A pod cannot reach the internet | the egress policy's reserved-CIDR `except:` list, held by `task lint:netpol-parity` |
| A Service VIP is unreachable from a peer selector | an `ipBlock` peer can never match a Service VIP: kube-proxy rewrites the destination first. Select the pods, not the address |
| A certificate is not renewing | the Certificate's conditions, then the issuer's. A per-host distribution target needs its pinned key seeded |
| A guest lost the network | the bond's slave state and the switch-side port, before suspecting the guest |
| A host-side change did not apply | re-run the owning playbook with `--limit <host>`; a looped task with a per-item `delegate_to` ends the play silently when a delegate is down |
| A node is NotReady | `task k3s:status`, then that node's kubelet journal through the log stack |
| Logs stopped arriving from a host | the host log-shipping role ran, and the ingress route it pushes through is up |
| An alert fired with no runbook | the rule's `runbook_url` must anchor a real heading; `task lint:prometheus-config` is the gate |
| A credential stopped working | re-run the task that injects it, or `task flux:rotate-secret -- <ns>/<name>` for an in-cluster one |

## Reading logs and metrics

Query the log stack rather than ssh-ing to each host: a host-level fault is
usually visible as a pattern across hosts, and the shipped journal is already
there. For a metric, the dashboard first, then the raw query — a dashboard panel
names the series a hand-written query would have to guess.

## Local gates that reproduce a CI failure

- `task lint` — the lint stage.
- `task flux:lint` — the rendered corpus, with no cluster access.
- `task lint:prometheus-config` — rules, promtool and amtool on PATH.
- `task ansible:lint` and `ansible-playbook --syntax-check` — the Ansible side.

Integration scenarios need Docker and a matching architecture. CI is the arbiter
for anything a container cannot do locally.
