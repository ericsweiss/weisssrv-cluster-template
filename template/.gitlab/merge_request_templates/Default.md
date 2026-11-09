<!--
Default merge-request template. Keep the Testing section as prose describing
what you actually ran, never a checklist of intentions.
-->

## Summary

<!-- One or two sentences: what this MR does and why. -->

## Changes

<!-- Bullet the notable changes. Group by area (ansible / kubernetes / terraform / scripts / CI / docs). -->

-

## Testing done

<!--
Prose, in the past tense, describing what was actually run and what it showed.
Never a checklist. For example:
"`task lint` was clean; `task flux:lint` validated the Flux corpus; the playbook
ran with `--check` against one host and the diff was empty."
-->

## Deploy notes

<!--
Anything the operator or reviewer must know before this reaches the cluster:
new secrets to create in the secret store, a host that must be deployed with
Ansible before Flux reconciles, a library pin bump that needs a re-vendor, a
manual Terraform apply, or how to roll back. "None" is a valid answer.
-->
