"""The SSO root re-exports every id a disaster-recovery import block takes.

README § Adoption and recovery reads the ids back out of state after an apply,
so an id the root does not output cannot reach imports.tf.
"""

from __future__ import annotations

import re

from conftest import REPO

ROOT = REPO / "terraform" / "authentik"

IMPORT_IDS = frozenset(
    {
        "application_ids",
        "oauth2_provider_ids",
        "proxy_provider_ids",
        "saml_provider_ids",
        "custom_scope_mapping_ids",
        "group_ids",
        "policy_binding_ids",
    }
)

_OUTPUT = re.compile(r'^output\s+"([^"]+)"\s*\{(.*?)^\}', re.MULTILINE | re.DOTALL)


def missing_reexports(outputs_tf: str) -> list[str]:
    """Import ids the root does not output as the module's own value."""
    bodies = dict(_OUTPUT.findall(outputs_tf))
    return sorted(
        name
        for name in IMPORT_IDS
        if not re.search(rf"value\s*=\s*module\.sso\.{name}\b", bodies.get(name, ""))
    )


def test_the_root_reexports_every_import_id() -> None:
    missing = missing_reexports((ROOT / "outputs.tf").read_text(encoding="utf-8"))
    assert not missing, (
        f"terraform/authentik/outputs.tf does not re-export {missing}, so a lost "
        "state re-creates those objects instead of re-importing them"
    )


def test_the_read_back_step_names_every_import_id() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    unnamed = sorted(name for name in IMPORT_IDS if f"`{name}`" not in readme)
    assert not unnamed, f"README § Adoption and recovery does not name {unnamed}"


def test_a_dropped_or_rewired_reexport_is_reported() -> None:
    """Mutation case: an output left out, or pointed at another value, fails."""
    block = 'output "{0}" {{\n  value = module.sso.{1}\n}}\n'
    full = "".join(block.format(name, name) for name in sorted(IMPORT_IDS))
    assert missing_reexports(full) == []
    dropped = full.replace(block.format("saml_provider_ids", "saml_provider_ids"), "")
    assert missing_reexports(dropped) == ["saml_provider_ids"]
    rewired = full.replace("module.sso.group_ids", "module.sso.application_ids")
    assert missing_reexports(rewired) == ["group_ids"]
