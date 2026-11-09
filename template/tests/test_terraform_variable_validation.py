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

# Every `regex("<pattern>", <value>)` call. The literal may hold escaped quotes,
# and the value may be any expression, such as a for-expression's element.
_REGEX_CALL = re.compile(r'regex\(\s*"(?P<pattern>(?:[^"\\]|\\.)*)"\s*,\s*(?P<value>[^)]*)\)')


def regex_validations(root: Path) -> list[tuple[Path, str, str]]:
    """(file, value expression, regex literal) for every regex call under `root`."""
    return [
        (path, match["value"].strip(), match["pattern"])
        for path in sorted(root.rglob("*.tf"))
        if ".terraform" not in path.relative_to(root).parts
        for match in _REGEX_CALL.finditer(path.read_text(encoding="utf-8"))
    ]


def drift_problems(root: Path, expected: set[str]) -> list[str]:
    """How the root's regex literals differ from its frozen set."""
    found = {pattern for _path, _value, pattern in regex_validations(root)}
    problems = []
    if extra := sorted(found - expected):
        problems.append(f"only in the tree: {extra}")
    if missing := sorted(expected - found):
        problems.append(f"only in the table: {missing}")
    return problems


def anchoring_problems(validations: list[tuple[Path, str, str]]) -> list[str]:
    """Literals unanchored at either end, which match part of a value."""
    return [
        f"{path.relative_to(REPO) if path.is_relative_to(REPO) else path}: {value} = {pattern!r}"
        for path, value, pattern in validations
        if not (pattern.startswith("^") and pattern.endswith("$"))
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
    problems = drift_problems(root, expected)
    assert not problems, (
        f"terraform/{root.name} validation regexes drifted\n  " + "\n  ".join(problems)
    )


@needs_terraform
def test_every_regex_validation_is_anchored() -> None:
    """`regex` matches anywhere in the value, so an unanchored rule accepts any
    string that merely contains a valid one."""
    loose = anchoring_problems(regex_validations(TERRAFORM))
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
    expected = {r"^[\\x20-\\x7e]{8,63}$"}
    assert drift_problems(root, expected) == ["only in the tree: ['^.{8,63}$']"]


def test_an_unanchored_regex_is_recognised(tmp_path) -> None:
    """Mutation case for the anchoring rule."""
    root = tmp_path / "loose"
    root.mkdir()
    (root / "variables.tf").write_text(
        'condition = can(regex("[0-9a-f]{32}", var.account_id))\n', encoding="utf-8"
    )
    assert len(anchoring_problems(regex_validations(root))) == 1


def test_the_extractor_reads_escaped_quotes_and_any_value(tmp_path) -> None:
    """Mutation case: an escaped quote does not end the literal, and a regex
    over a for-expression element is still a validation."""
    root = tmp_path / "dns"
    root.mkdir()
    (root / "variables.tf").write_text(
        'condition = alltrue([for h in var.hosts : can(regex("^\\"[a-z]+\\"$", h))])\n',
        encoding="utf-8",
    )
    assert [(value, p) for _path, value, p in regex_validations(root)] == [
        ("h", '^\\"[a-z]+\\"$')
    ]
