"""Read backticked names out of the first cell of a Markdown table.

Two gates walk template/scripts/README.md for the same shape, so the row pattern
lives here rather than once per caller.
"""

from __future__ import annotations

import re

_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|")
_BACKTICKED = re.compile(r"`([^`]+)`")


def table_names(text: str, heading: str, suffixes: tuple[str, ...] = (".py", ".sh")) -> set[str]:
    """Backticked names with one of `suffixes`, from rows under `heading`."""
    names: set[str] = set()
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == heading
            continue
        row = _ROW.match(line) if in_section else None
        if row:
            names.update(n for n in _BACKTICKED.findall(row.group(1)) if n.endswith(suffixes))
    return names
