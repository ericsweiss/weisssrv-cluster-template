"""Input-validation regexes in the Terraform roots are frozen, per root.

Two roots repeat the same literal, so the table below is the contract: adding
or loosening a validation is a visible edit here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import REPO

TERRAFORM = REPO / "terraform"

needs_terraform = pytest.mark.skipif(
    not TERRAFORM.is_dir(), reason="this cluster ships no terraform/ roots"
)

# Distinct regex literals per root, exactly as the .tf files spell them. A root
# this cluster's answers did not generate is simply absent from the tree.
EXPECTED_PATTERNS: dict[str, set[str]] = {
    "authentik": {"^https://[a-z0-9.-]+$"},
    "cloudflare": {
        "^[0-9a-f]{32}$",
        r"^[a-z0-9-]+(\\.[a-z0-9-]+)*\\.[a-z]{2,}$",
    },
    "tailscale": set(),
    "unifi": {
        "^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$",
        r"^[\\x20-\\x7e]{8,63}$",
    },
}

# `can(regex("<pattern>", var.<name>))` — the only validation shape the roots use.
_REGEX_VALIDATION = re.compile(r'can\(regex\("(?P<pattern>.*?)",\s*var\.(?P<name>\w+)\)\)')


def regex_validations(root: Path) -> list[tuple[Path, str, str]]:
    """(file, variable name, regex literal) for every regex validation under `root`."""
    return [
        (path, match["name"], match["pattern"])
        for path in sorted(root.rglob("*.tf"))
        for match in _REGEX_VALIDATION.finditer(path.read_text(encoding="utf-8"))
    ]


def terraform_roots() -> list[Path]:
    """The roots this cluster generated: a directory holding a .tf file."""
    if not TERRAFORM.is_dir():
        return []
    return sorted(p for p in TERRAFORM.iterdir() if p.is_dir() and any(p.glob("*.tf")))


@needs_terraform
def test_the_walk_finds_the_validations() -> None:
    """A changed validation shape would make every assertion below vacuous."""
    roots = terraform_roots()
    assert roots, f"{TERRAFORM.name}/ ships no root with a .tf file"
    assert regex_validations(TERRAFORM), (
        "no `can(regex(...))` validation under terraform/ — either the roots "
        "stopped validating their inputs, or the shape changed and this gate is "
        "examining nothing."
    )


@needs_terraform
def test_every_generated_root_is_in_the_table() -> None:
    """A new root joins the contract rather than arriving unfrozen."""
    unlisted = sorted(r.name for r in terraform_roots() if r.name not in EXPECTED_PATTERNS)
    assert not unlisted, (
        f"terraform roots with no EXPECTED_PATTERNS entry: {unlisted} — add the "
        "root with the regex literals its variables validate against."
    )


@needs_terraform
@pytest.mark.parametrize("root", terraform_roots(), ids=lambda p: p.name)
def test_each_root_validates_with_the_frozen_regexes(root: Path) -> None:
    """A copied literal that drifts shows up here as an extra pattern."""
    expected = EXPECTED_PATTERNS.get(root.name)
    if expected is None:
        pytest.skip(f"{root.name} is reported by the table check")
    found = {pattern for _path, _name, pattern in regex_validations(root)}
    assert found == expected, (
        f"terraform/{root.name} validation regexes drifted\n"
        f"  only in the tree:  {sorted(found - expected)}\n"
        f"  only in the table: {sorted(expected - found)}"
    )


@needs_terraform
def test_every_regex_validation_is_anchored() -> None:
    """`regex` matches anywhere in the value, so an unanchored rule accepts any
    string that merely contains a valid one."""
    loose = [
        f"{path.relative_to(REPO)}: {name} = {pattern!r}"
        for path, name, pattern in regex_validations(TERRAFORM)
        if not (pattern.startswith("^") and pattern.endswith("$"))
    ]
    assert not loose, "unanchored input validations:\n  " + "\n  ".join(loose)


def test_a_drifted_copy_is_reported(tmp_path) -> None:
    """Mutation case: a second member of a repeated rule, loosened."""
    root = tmp_path / "unifi"
    root.mkdir()
    (root / "variables.tf").write_text(
        'variable "wlan_passphrase_iot" {\n'
        "  validation {\n"
        '    condition = can(regex("^[\\\\x20-\\\\x7e]{8,63}$", var.wlan_passphrase_iot))\n'
        "  }\n"
        "}\n"
        'variable "wlan_passphrase_guest" {\n'
        "  validation {\n"
        '    condition = can(regex("^.{8,63}$", var.wlan_passphrase_guest))\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    found = {pattern for _path, _name, pattern in regex_validations(root)}
    assert found == {r"^[\\x20-\\x7e]{8,63}$", "^.{8,63}$"}
    assert found != EXPECTED_PATTERNS["unifi"]


def test_an_unanchored_regex_is_recognised(tmp_path) -> None:
    """Mutation case for the anchoring rule."""
    root = tmp_path / "loose"
    root.mkdir()
    (root / "variables.tf").write_text(
        'condition = can(regex("[0-9a-f]{32}", var.account_id))\n', encoding="utf-8"
    )
    patterns = [p for _path, _name, p in regex_validations(root)]
    assert patterns == ["[0-9a-f]{32}"]
    assert not (patterns[0].startswith("^") and patterns[0].endswith("$"))
