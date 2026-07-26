# gitlab-runner-reaper

A CronJob that deletes **leaked** GitLab Runner job pods and their orphaned
per-job image-pull Secrets, in both runner namespaces.

## Why it exists

The runner manager normally deletes each job's executor pod and its
`kubernetes.io/dockercfg` credentials Secret when the job ends. When many jobs
fail or get cancelled at once, or the manager restarts mid-cleanup, both leak.
GitLab Runner has no built-in GC for that case — the manager-side `cleanup_*`
settings only run on the graceful end-of-job path, never when the manager itself
dies during it.

Leaked pods bloat etcd, add scheduler churn, consume the namespace's `pods`
ResourceQuota dimension (which then 403s the manager's *next* job pod), and trip
maintenance health scans. Leaked Secrets pile up as registry-credential blobs
with no owning job.

## Why it is safe to run

Deleting pods on a schedule is the kind of automation that eats a production
workload if it is even slightly wrong. Three **independent** guards apply to
pods — any one alone already excludes a manager pod:

1. The `<node-label-domain>/runner-class` label must EXIST. Every executor pod
   carries it (set via `pod_labels` in both runner releases); manager pods never
   do, so a label-existence selector is structurally incapable of matching one.
2. Phase must be `Succeeded` or `Failed`. A manager is `Running`; an in-flight
   job pod is `Running` or `Pending`. All unselectable.
3. The name must match `runner-*-project-*-concurrent-*`.

Plus a grace rule: a terminal pod is deleted only if its NEWEST container
termination time is older than `MAX_AGE_MINUTES`, measured from container
`finishedAt` — **never** from pod creation, which can be hours older than
completion and would delete a freshly-finished long job. If any started container
lacks a parseable `finishedAt`, the pod is KEPT.

Secrets get their own three guards: type is narrowed **server-side** to
`kubernetes.io/dockercfg` (so the SA never lists runner tokens or SA tokens at
all), the name must start with `runner-`, and the Secret must not be referenced
by any live pod via `imagePullSecrets` or `ownerReferences`. Their age runs from
`creationTimestamp` with a much longer floor (3h), which structurally protects
any in-flight job's Secret and closes the create-Secret-then-create-pod race.

## RBAC

Kubernetes RBAC cannot scope `delete` by label, phase or type, so `pods: delete`
+ `secrets: delete` in the two runner namespaces is the least grant that performs
this function. It is a ClusterRole bound by two **namespaced** RoleBindings —
never a ClusterRoleBinding — so there is no cluster-wide pod or secret delete
anywhere. Verbs are `list` and `delete` only: no `get`, no create/update/patch,
no configmaps.

`list secrets` returns Secret data (Kubernetes has no list-without-data). The
server-side type filter keeps that to the ephemeral per-job registry credentials;
the residual exposure is inherent and is mitigated by the digest-pinned image,
the fixed no-input script, and this dedicated single-purpose ServiceAccount.

## Operational shape

- Runs every 15 minutes, `concurrencyPolicy: Forbid`.
- Lists are **paged** so a large backlog cannot OOM the 64Mi container.
- A soft `BUDGET_SECONDS` stops cleanly under `activeDeadlineSeconds`: partial
  progress this run, the rest next run — rather than a hard deadline-kill that
  marks the Job failed.
- Any non-race list/delete error exits non-zero, so a persistent failure (broken
  RBAC) surfaces as a failed Job instead of a silent no-op.
- `ttlSecondsAfterFinished` on its own Jobs, so the reaper does not itself leak.

## Disable

Remove the entry from `kubernetes/apps/kustomization.yaml`. Then watch the `pods`
quota dimension in both runner namespaces — a leak backlog eventually 403s new
job pods.
