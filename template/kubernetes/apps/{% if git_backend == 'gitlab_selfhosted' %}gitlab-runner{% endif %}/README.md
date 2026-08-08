# gitlab-runner (shared, non-privileged)

The **default** runner: it picks up untagged jobs from any project on the GitLab
instance. Kubernetes executor, non-root, no Docker-in-Docker, egress-contained.

Its sibling `gitlab-runner-privileged` is the trusted tier. Read that module's
README before you touch either — the split between them is a security boundary,
not an optimisation.

## What contains an untrusted job here

| Control | Effect |
|---|---|
| `pod_security_context` non-root (uid/gid 1000) | job code never runs as root |
| namespace PSS `enforce: baseline` | admission blocks privileged / host\* escalation |
| `gitlab-runner-jobs` ServiceAccount | no RoleBinding, no mounted token — a job cannot read the runner token Secret, other jobs' pod specs (which carry CI variables), or exec into the manager |
| `shared-jobs-egress` NetworkPolicy | internet only: RFC1918 and the LAN are blocked; DNS, the kube-API and GitLab-via-ingress are the exceptions |
| namespace-wide `default-deny-egress` | a job that uses its `pods:create` RBAC to spawn an *unlabelled* auxiliary pod gets no egress at all, instead of unrestricted |
| ResourceQuota + LimitRange | a job cannot starve resident workloads |

`protected: false` is an accepted risk: it lets the runner accept jobs from
unprotected branches, which is what makes MR pipelines work. The containment
above is what makes that acceptable. In a team environment, set `protected: true`.

## Sizing

`concurrent` must stay at or below what the ResourceQuota admits. A job pod sums
to ~5Gi of memory limits, and the manager counts too — so a 40Gi limit admits
about 7. Set it higher and the runner submits pods the quota rejects with a 403,
which GitLab surfaces as a spurious job failure with no retry. At the right
value, excess jobs wait in GitLab's queue instead. **Move `concurrent` and the
quota together.**

## Configure

| Value | Where |
|---|---|
| Chart version | `gitlab_runner_helm_version` |
| GitLab URL | `${cluster_git_host}` (use the internally-resolvable name) |
| Runner token | secrets item **GitLab Runner** → field `runner-token` |
| Job image | `runners.config` TOML, digest-pinned |
| Tag | `k8s-deploy` (plus untagged) |

The token is a `glrt-*` authentication token. The chart's projected-secrets
volume needs BOTH `runner-token` and `runner-registration-token` keys even though
the legacy registration flow is unused — the ExternalSecret templates the second
one as an empty string so the mount succeeds.

## Disable

Remove the entry from `kubernetes/apps/kustomization.yaml`. Jobs then have no
runner unless you register one elsewhere.
