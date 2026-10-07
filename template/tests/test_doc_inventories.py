"""README.md § Reference is the index of docs/, and it must list every file there.

A doc nobody links is a doc nobody reads, and every alert's runbook is reached
through that index.
"""

from __future__ import annotations

from conftest import REPO

README = REPO / "README.md"
DOCS = REPO / "docs"


def reference_section(text: str) -> str:
    """`## Reference` up to the next `## ` heading, or to the end of the file.

    Scoped so that a `docs/` link anywhere else in the README cannot satisfy the
    index assertion: § Bring-up and § Everyday operations both carry some.
    """
    start = text.index("\n## Reference\n")
    rest = text[start + 1 :]
    end = rest.find("\n## ", 1)
    return rest if end < 0 else rest[:end]


def unindexed(section: str, names: list[str]) -> list[str]:
    return sorted(name for name in names if f"](docs/{name})" not in section)


def _shipped_docs() -> list[str]:
    return sorted(path.name for path in DOCS.glob("*.md"))


def test_readme_indexes_every_doc():
    missing = unindexed(reference_section(README.read_text(encoding="utf-8")), _shipped_docs())
    assert not missing, (
        "docs/ files with no row in README.md § Reference:\n  " + "\n  ".join(missing)
    )


def test_readme_index_scan_finds_the_real_docs():
    """Guard the slice: an empty or mis-sliced section would pass vacuously."""
    shipped = _shipped_docs()
    assert shipped, "docs/ holds no markdown — this gate is examining nothing"
    section = reference_section(README.read_text(encoding="utf-8"))
    assert "](docs/RUNBOOKS.md)" in section
    assert section.count("](docs/") >= len(shipped)


def test_a_link_outside_the_section_does_not_count():
    """Mutation case: the slice is what makes the index assertion mean anything."""
    readme = (
        "# Cluster\n"
        "\n## Bring-up\n"
        "\n- [docs/elsewhere.md](docs/elsewhere.md)\n"
        "\n## Reference\n"
        "\n- [docs/RUNBOOKS.md](docs/RUNBOOKS.md)\n"
    )
    section = reference_section(readme)
    assert unindexed(section, ["RUNBOOKS.md"]) == []
    assert unindexed(section, ["elsewhere.md"]) == ["elsewhere.md"]
