<!--
Default merge-request template. Keep the Testing section as prose describing
what you actually ran — not a checklist of intentions.
-->

## Summary

<!-- One or two sentences: what this MR does and why. -->

## Changes

<!-- Bullet the notable changes. Group by area (copier.yml / template / docs / CI). -->

-

## Testing done

<!--
Describe the verification you performed, in prose. For example:
"`python3 -m pytest tests -q` clean; rendered both fixtures and ran
`tests/validate_render.py --lib-path ~/src/weisssrv-lib` on each, which
yamllints, kubeconforms the Flux corpus and syntax-checks the playbooks."
-->

## Consumer impact

<!--
This repository is an API: every question is replayed on `copier update` in
every generated cluster. Name what a consumer has to do, or write "none".

  - a renamed, removed or newly REQUIRED question is breaking
  - a `lib_ref` bump means the fixture, copier.yml's default, this repo's own
    `include:` refs and docs/VERSIONING.md's validated-pair table move together
  - a changed backend seam (git / secrets / storage / dns) needs its row in
    docs/ARCHITECTURE.md "Backend seams"
-->
