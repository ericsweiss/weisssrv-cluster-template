#!/usr/bin/env python3
"""One argv walk over the `ansible-playbook` calls in a pipeline job script.

Shared by every deploy gate so they all see the same set of calls, with the
same inventory, limit and tag semantics. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import re
import shlex

INVENTORY_FLAGS = frozenset({"-i", "--inventory", "--inventory-file"})
LIMIT_FLAGS = frozenset({"-l", "--limit"})
TAGS_FLAGS = frozenset({"-t", "--tags"})
SKIP_TAGS_FLAGS = frozenset({"--skip-tags"})
# Consumed so their value is never mistaken for the playbook argument.
OTHER_VALUE_FLAGS = frozenset({"-e", "--extra-vars"})
VALUE_FLAGS = INVENTORY_FLAGS | LIMIT_FLAGS | TAGS_FLAGS | SKIP_TAGS_FLAGS | OTHER_VALUE_FLAGS

PLAYBOOK_SUFFIXES = (".yml", ".yaml")


def _flag(token: str) -> tuple[str, str | None]:
    """Split `--flag=value` into its parts; other tokens keep a None value."""
    if token.startswith("-") and "=" in token:
        name, _, value = token.partition("=")
        return name, value
    return token, None


def _attached(token: str, flags) -> tuple[str, str] | None:
    """A short flag with its value attached, as in `-iprod` or `-lproxmox`."""
    for flag in flags:
        if len(flag) == 2 and token.startswith(flag) and len(token) > 2:
            return flag, token[2:]
    return None


def parse_invocations(text: str) -> list[dict]:
    """Every `ansible-playbook` call in `text`, one dict per call.

    `tags` and `skip_tags` stay apart, so a `--skip-tags` value is never read
    as a selection. Keys: docs/SCRIPTS.md.
    """
    calls: list[dict] = []
    for segment in text.split("ansible-playbook")[1:]:
        # A continued line is still this argv; an unescaped newline ends it.
        segment = segment.replace("\\\n", " ")
        segment = re.split(r"[;&|\n]", segment, maxsplit=1)[0]
        try:
            tokens = shlex.split(segment)
        except ValueError:
            # An unbalanced quote is the tail of the enclosing `bash -c '...'`.
            tokens = segment.replace('"', " ").replace("'", " ").split()
        call: dict = {
            "inventory": None,
            "playbook": None,
            "limit": None,
            "tags": None,
            "skip_tags": None,
            "argv": " ".join(tokens),
        }
        index = 0
        while index < len(tokens):
            token = tokens[index]
            name, value = _flag(token)
            if name not in VALUE_FLAGS:
                attached = _attached(token, VALUE_FLAGS)
                if attached:
                    name, value = attached
            if name in VALUE_FLAGS:
                if value is None:
                    value = tokens[index + 1] if index + 1 < len(tokens) else None
                    index += 1
                if name in INVENTORY_FLAGS:
                    call["inventory"] = value
                elif name in LIMIT_FLAGS:
                    call["limit"] = value
                elif value and (name in TAGS_FLAGS or name in SKIP_TAGS_FLAGS):
                    key = "tags" if name in TAGS_FLAGS else "skip_tags"
                    call[key] = (call[key] or set()) | {t for t in value.split(",") if t}
                index += 1
                continue
            if (
                call["playbook"] is None
                and not token.startswith("-")
                and token.endswith(PLAYBOOK_SUFFIXES)
            ):
                call["playbook"] = token
            index += 1
        if call["playbook"]:
            calls.append(call)
    return calls
