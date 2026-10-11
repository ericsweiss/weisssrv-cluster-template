#!/usr/bin/env python3
"""Parse a .gitlab-ci.yml that uses GitLab's custom `!` tags.

`CILoader` keeps a tagged node's structure and turns `!reference` into a
resolvable `Reference`; `NullTagCILoader` nulls them. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None


class Reference(list):
    """A resolved-on-demand `!reference [.job, script]` node.

    Subclasses list so a caller that only wants the key path (or that iterates
    the parse looking for strings) sees the keys and never crashes.
    """

    def resolve(self, doc: Any, default: Any = None) -> Any:
        """Follow the key path through `doc`; `default` when it does not lead
        anywhere (a reference to a job defined in an included file)."""
        node = doc
        for key in self:
            if isinstance(node, dict) and key in node:
                node = node[key]
            elif isinstance(node, list) and isinstance(key, int) and key < len(node):
                node = node[key]
            else:
                return default
        return node


def _passthrough(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return None


def _reference(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return Reference(loader.construct_sequence(node, deep=True))
    return _passthrough(loader, suffix, node)


class CILoader(yaml.SafeLoader):
    """Structure-preserving loader: `!reference` becomes a `Reference`, any
    other tagged node keeps its scalar/sequence/mapping shape."""


class NullTagCILoader(yaml.SafeLoader):
    """Every `!`-tagged node becomes None, so a walker skips it outright."""


# Order matters: PyYAML takes the FIRST matching tag prefix, so the
# "!reference" registration must precede the catch-all "!".
CILoader.add_multi_constructor("!reference", _reference)
CILoader.add_multi_constructor("!", _passthrough)
NullTagCILoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def split_header(text: str, loader: type = CILoader) -> tuple[Any, str]:
    """-> (the `spec:` header document, the body TEXT it heads).

    GitLab's inputs syntax makes a pipeline file two documents, `spec:` then the
    jobs. (None, the whole text) when there is no header.
    """
    docs = list(yaml.load_all(text, Loader=loader))
    if len(docs) < 2 or not (isinstance(docs[0], dict) and "spec" in docs[0]):
        return None, text
    starts = [
        event.start_mark.index
        for event in yaml.parse(text, Loader=loader)
        if isinstance(event, yaml.DocumentStartEvent)
    ]
    if len(starts) < 2:
        return None, text
    return docs[0], text[starts[1]:]


def spec_inputs(text: str, loader: type = CILoader) -> dict:
    """The header's `spec: inputs:` mapping; empty when the file has none."""
    header, _ = split_header(text, loader)
    inputs = ((header or {}).get("spec") or {}).get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def parse_ci(text: str, loader: type = CILoader) -> dict:
    """The jobs document of pipeline YAML already in memory.

    A `spec:` header is split off first (see split_header), so a caller always
    reads the body.
    """
    _, body = split_header(text, loader)
    docs = [d for d in yaml.load_all(body, Loader=loader) if isinstance(d, dict)]
    return docs[-1] if docs else {}


def load_ci(path, loader: type = CILoader) -> dict:
    return parse_ci(Path(path).read_text(encoding="utf-8"), loader=loader)


def jobs(doc: dict) -> dict:
    """The real jobs: mapping values whose key is not a `.hidden` template and
    not one of GitLab's reserved top-level keys."""
    reserved = {
        "default", "include", "stages", "variables", "workflow", "image",
        "services", "before_script", "after_script", "cache", "spec",
    }
    return {
        name: body
        for name, body in doc.items()
        if isinstance(body, dict) and not name.startswith(".") and name not in reserved
    }


_MISSING = object()


def script_lines(job: dict, doc: dict, key: str = "script",
                 unresolved: List[list] = None) -> List[str]:
    """A job's `key` block flattened to strings, `!reference` nodes expanded.

    A reference into an included file contributes nothing; pass `unresolved` to
    collect those key paths instead of losing them silently.
    """
    out: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Reference):
            target = value.resolve(doc, _MISSING)
            if target is _MISSING:
                if unresolved is not None:
                    unresolved.append(list(value))
                return
            walk(target)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif value is not None:
            out.append(str(value))

    walk(job.get(key) or [])
    return out
