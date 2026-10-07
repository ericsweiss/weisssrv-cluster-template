# Add a Proxmox guest

A guest is a VM or an LXC container that Ansible provisions and the cluster
fronts. Canonical procedure: `docs/RUNBOOKS.md`.

## Checklist

1. **Inventory first.** A host entry under the right group in
   `ansible/inventories/prod/hosts.yml`, with its facts inline: address, vmid,
   the Proxmox host it lives on, its firewall aliases and security groups. There
   is no `host_vars/` tree unless you create one.
2. **LXC or VM.** An LXC is cheaper and shares the host kernel; a VM is required
   for its own kernel, for passthrough, or for a non-Debian guest. An LXC
   application host must also be listed under `lxc_containers`: the maintenance
   reboot play asserts it found an installed kernel, and an LXC has none.
3. **Storage** is a zvol declared in the host entry's `vm_additional_disks`
   block, created by the provisioning role and mounted by the mount role. The
   zvol outlives the guest on top of it.
4. **Firewall.** Guest rules come from the host entry's security groups. A
   VIP-destined frame is FORWARDED to the announcing node's guest, so the GUEST
   firewall filters it and a datacenter-level rule is inert for it. That is the
   trap worth remembering.
5. **Secrets** reach the guest as environment variables resolved by the task's
   own secret runner, never as a committed value.
6. **Routing.** An in-cluster ingress fronts the guest through the `vm-ingress`
   application, so a routing change in `kubernetes/` can break a guest that
   Ansible never touched.
7. **Observability.** The guest runs the host metrics and log-shipping roles. Add
   it to the groups the site playbook applies those to, and keep a standalone
   playbook's role list in step with the site playbook.
8. **Deploy path.** A playbook under `ansible/playbooks/`, a `task` wrapper that
   injects its secrets, and either a CI deploy job or an entry in
   `scripts/deploy-coverage.conf` saying what deploys it instead. The coverage
   gates inside `task lint` fail a guest with neither.
9. **Backups.** A new top-level dataset is enrolled by naming it in the storage
   host's archive-backup source list; children of an already-enrolled dataset are
   picked up automatically.

## Before deploying

`task infra:check -- --limit <host>` dry-runs the change. Provisioning plays are
not idempotent in dry-run when the guest does not exist yet, so a first create is
read as inconclusive rather than failing.
