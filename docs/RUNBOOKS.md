# Runbooks

The day-two runbooks are **shipped into every generated cluster** at
`docs/RUNBOOKS.md`. They are not maintained as a second copy here, because every
alert rule the template ships annotates itself with

```
runbook_url: ${cluster_runbook_base_url}/RUNBOOKS.md#<section>
```

which resolves to that file in your cluster repository. A runbook that lives
only in the template repository would 404 at 03:00, and a copy in both places
would drift.

**Source**: [`template/docs/RUNBOOKS.md.jinja`](../template/docs/RUNBOOKS.md.jinja)
— read it here before generating, or read the rendered copy in your own
repository afterwards.

## What it covers

| Section | Anchor | Linked from |
|---|---|---|
| Where to look first | `#where-to-look-first` | every alert with no more specific runbook |
| The change workflow | — | — |
| Making Flux act now | — | — |
| Deploying an Ansible change | — | — |
| Upgrades (versions, host packages, k3s nodes, the library) | — | — |
| Adding a node | — | — |
| Adding an application | — | — |
| Rotating a secret | — | — |
| Certificates | `#certificates` | the certificate-expiry alerts |
| Suspending, rolling back, breaking glass | — | — |
| Storage | — | — |
| Backups and restore | `#backups-and-restore` | the backup-failed / stale alerts |
| When Flux is unhappy | — | — |
| Post-failover reconciliation | — | — |
| Updating the template | — | — |
| Where the platform is documented | — | — |

The three anchors are a contract with the alert rules in
`template/kubernetes/infrastructure/observability/`. Renaming one of those
headings breaks the links; if you rename it, change the annotations in the same
commit.

## Anchors are load-bearing, and so are task names

Every command in that file is one the generated `Taskfile.yml` defines. The
render tests (`tests/test_render.py::test_documented_tasks_exist`) walk the
generated Markdown and fail on any `task <name>` the generated Taskfile does not
define, so a renamed or dropped task cannot silently leave a broken procedure
behind.

The same applies to this repository's own documents:
[SETUP.md](SETUP.md) and [PRE-SETUP.md](PRE-SETUP.md) name generated tasks too,
and are held to the same list.

## Related

- [ARCHITECTURE.md](ARCHITECTURE.md) — why the shapes are what they are
- [SETUP.md](SETUP.md) — first bring-up, which the runbooks pick up from
- [CI.md](CI.md) — what runs in the pipeline
- [VERSIONING.md](VERSIONING.md) — what a template release may change, and what
  `copier update` does with it (the § *Updating the template* runbook is the
  procedure; this is the contract behind it)
