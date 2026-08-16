# CLAUDE.md

Guidance for Claude Code (and other agents) working in
**weisssrv-cluster-template**, the copier template that generates a complete
Proxmox + ZFS + k3s GitOps cluster repository.

Nothing here is deployed — the OUTPUT is. A change reaches every generated
cluster through `copier update`, so the blast radius of an edit is every
cluster, not this repo.

[AGENTS.md](AGENTS.md) carries the standing rules for this repository: what to
read before changing a question, a manifest, a seam, the library pin or a
vendored script; the gate commands to run before proposing a change; and the
house rules. Read it before changing anything. It is not duplicated here.
