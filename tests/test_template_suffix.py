"""Keep the `.jinja` suffix honest in both directions.

tests/test_render.py catches a templated file that LOST the suffix. This module
catches the reverse: a file carrying the suffix while holding no Jinja at all.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import render_cluster

REPO_ROOT = render_cluster.REPO_ROOT
SUFFIX = yaml.safe_load((REPO_ROOT / "copier.yml").read_text(encoding="utf-8"))[
    "_templates_suffix"
]

JINJA_MARKERS = ("{{", "{%", "{#")


def _templated_sources() -> list[Path]:
    return sorted(
        path
        for path in (REPO_ROOT / "template").rglob(f"*{SUFFIX}")
        if path.is_file()
    )


def _is_templated(text: str) -> bool:
    return any(marker in text for marker in JINJA_MARKERS)


def test_every_templated_file_contains_jinja():
    """A suffixed file with no Jinja renders byte-identically to its source, so
    the suffix only misleads. Drop it, or say why it stays in a `{# #}` comment.
    """
    sources = _templated_sources()
    assert sources, "no templated sources found under template/"
    inert = [
        str(path.relative_to(REPO_ROOT))
        for path in sources
        if not _is_templated(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not inert, (
        f"these files carry the {SUFFIX} suffix but hold no Jinja:\n  "
        + "\n  ".join(inert)
    )


def test_the_scan_rejects_an_inert_source(tmp_path):
    """Negative case: the predicate the gate runs on must fail a plain file."""
    assert not _is_templated("plain text\n")
    assert _is_templated("{{ cluster_name }}\n")
    assert _is_templated("{% if use_unifi %}x{% endif %}\n")
    assert _is_templated("{#- deliberate -#}\nplain text\n")
