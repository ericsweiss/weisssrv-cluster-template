#!/usr/bin/env python3
"""Fail a comment block over three content lines (eight when it opens "CRITICAL:").

Scans the paths given, else the config's `paths`, else the working directory; exits 0
clean, 1 on an over-long block, 2 on an operator error. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import sys
from pathlib import Path

MAX_LINES = 3
CRITICAL_MAX_LINES = 8
CRITICAL_PREFIX = "CRITICAL:"

# Line-comment markers and block delimiters per suffix. A suffix absent from
# both maps is skipped, so a new language opts in deliberately.
LINE_MARKERS = {
    ".bash": ("#",),
    ".cfg": ("#",),
    ".conf": ("#",),
    ".go": ("//",),
    ".hcl": ("#", "//"),
    ".ini": ("#",),
    # A .j2 renders into whatever language its consumer needs, so both markers.
    ".j2": ("#", "//"),
    ".js": ("//",),
    ".py": ("#",),
    ".sh": ("#",),
    ".tf": ("#", "//"),
    ".tfvars": ("#", "//"),
    ".toml": ("#",),
    ".ts": ("//",),
    ".yaml": ("#",),
    ".yml": ("#",),
}
BLOCK_DELIMITERS = {
    ".go": (("/*", "*/"),),
    ".hcl": (("/*", "*/"),),
    ".j2": (("{#", "#}"),),
    ".js": (("/*", "*/"),),
    ".tf": (("/*", "*/"),),
    ".tfvars": (("/*", "*/"),),
    ".ts": (("/*", "*/"),),
}
# Extension-less files the repo authors comments in, keyed by basename because
# Path("Dockerfile").suffix is empty.
NAME_MARKERS = {
    ".ansible-lint": ("#",),
    ".ansible-lint-ignore": ("#",),
    ".editorconfig": ("#",),
    ".gitattributes": ("#",),
    ".gitignore": ("#",),
    ".yamllint": ("#",),
    "Dockerfile": ("#",),
    "editorconfig": ("#",),
    "gitattributes": ("#",),
}


# Copier renders a template by dropping this suffix, so `main.tf.jinja` is
# resolved as `main.tf`. A conditional path component goes with it: left in
# place, `{% if x %}ci.yml{% endif %}` reads as the suffix `.yml{% endif %}`.
TEMPLATE_SUFFIX = ".jinja"
JINJA_STATEMENT = re.compile(r"\{%.*?%\}", re.S)
JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)
# `${#` is bash's array length, not a comment opener.
JINJA_COMMENT_TAG = re.compile(r"(?<!\$)\{#.*?#\}", re.S)
# A `{% raw %}` body is literal text: jinja reads no tags inside it, and an
# f-string's `{{` there is Python.
JINJA_RAW = re.compile(
    r"(?P<open>\{%-?\s*raw\s*-?%\})(?P<body>.*?)(?P<close>\{%-?\s*endraw\s*-?%\})",
    re.S,
)
# A .jinja source carries `{# #}` comments whatever language it renders into.
JINJA_BLOCK = (("{#", "#}"),)

# Fenced-code languages resolved to the suffix the marker tables are keyed on.
# A language absent here is skipped, so a new one opts in deliberately.
FENCE_LANGUAGES = {
    "bash": ".sh",
    "cfg": ".cfg",
    "conf": ".conf",
    "console": ".sh",
    "dockerfile": "Dockerfile",
    "go": ".go",
    "hcl": ".tf",
    "ini": ".ini",
    "javascript": ".js",
    "jinja": ".j2",
    "js": ".js",
    "j2": ".j2",
    "py": ".py",
    "python": ".py",
    "sh": ".sh",
    "shell": ".sh",
    "terraform": ".tf",
    "tf": ".tf",
    "toml": ".toml",
    "ts": ".ts",
    "typescript": ".ts",
    "yaml": ".yaml",
    "yml": ".yaml",
    "zsh": ".sh",
}
# Markdown carries no comments of its own; `--scan-fenced-code` reaches the
# snippets inside it, which are copied into real files.
MARKDOWN_SUFFIXES = (".md", ".markdown")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)")

# A Dockerfile's leading `# syntax=` / `# escape=` lines are parser directives:
# machine-readable config, so they neither count as content nor join the header
# block beneath them. The format allows them only at the top of the file.
PARSER_DIRECTIVE = re.compile(r"^#\s*(?:syntax|escape)\s*=", re.IGNORECASE)
JINJA_TAG_LINE = re.compile(r"^\{[%#].*[%#]\}$")


def _rendered_name(path: Path) -> Path:
    """The name a template source renders into: `main.tf.jinja` -> `main.tf`,
    `{% if x %}ci.yml{% endif %}.jinja` -> `ci.yml`."""
    name = path.name
    if name.endswith(TEMPLATE_SUFFIX) and len(name) > len(TEMPLATE_SUFFIX):
        name = JINJA_STATEMENT.sub("", name[: -len(TEMPLATE_SUFFIX)]) or name
    return Path(name)


def suffix_for(path: Path) -> str:
    """The suffix the per-language tables are keyed on, after a template suffix."""
    return _rendered_name(path).suffix


def markers_for(path: Path):
    """Line-comment markers for a path, by suffix and then by basename. A
    stage-suffixed `Dockerfile.<name>` resolves to the plain Dockerfile markers."""
    name = _rendered_name(path)
    found = LINE_MARKERS.get(name.suffix) or NAME_MARKERS.get(name.name)
    if found:
        return found
    if name.name.startswith("Dockerfile."):
        return NAME_MARKERS["Dockerfile"]
    return None


def takes_parser_directives(path: Path) -> bool:
    """Whether this file's format reads leading `# syntax=` / `# escape=` lines
    as parser directives rather than as comments."""
    name = _rendered_name(path).name
    return name == "Dockerfile" or name.startswith("Dockerfile.")

# Tool caches, scratch trees and third-party trees a repo does not author.
# Written without a leading `*/` so they also match at the root of a relative
# scan; matches() tries each one as `*/<pattern>` too.
DEFAULT_EXCLUDES = (
    ".ansible-home/*",
    ".ansible/*",
    ".git/*",
    ".mypy_cache/*",
    ".pytest_cache/*",
    ".ruff_cache/*",
    ".terraform/*",
    ".tmp/*",
    ".venv/*",
    ".worktrees/*",
    "*.egg-info/*",
    "__pycache__/*",
    "node_modules/*",
)


class Limits:
    """The line budget a plain block gets, and the one a CRITICAL: block gets."""

    def __init__(self, plain: int = MAX_LINES, critical: int = CRITICAL_MAX_LINES):
        self.plain = plain
        self.critical = critical

    def limit_for(self, first_line: str) -> int:
        return self.critical if first_line.startswith(CRITICAL_PREFIX) else self.plain


class Finding:
    """One over-long comment block: where it starts, how long, its first line."""

    def __init__(self, path: Path, line: int, length: int, limit: int, first: str):
        self.path = path
        self.line = line
        self.length = length
        self.limit = limit
        self.first = first

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.line}: comment block is {self.length} content lines "
            f"(limit {self.limit}): {self.first[:60]}"
        )


def _strip_markers(text: str, markers: tuple) -> str:
    stripped = text.strip()
    for marker in markers:
        if stripped.startswith(marker):
            return stripped[len(marker) :].strip()
    return stripped


def _record(findings: list, path: Path, line: int, body: list, limits: Limits) -> None:
    content = [ln for ln in body if ln]
    first = content[0] if content else ""
    limit = limits.limit_for(first)
    if len(content) > limit:
        findings.append(Finding(path, line, len(content), limit, first))


def line_comment_findings(
    path: Path, text: str, markers: tuple, limits: Limits, directives: bool = False
) -> list:
    """Runs of whole-line comments longer than their limit.

    A trailing comment on a code line is one line by construction, so only
    lines that are comments end to end are grouped into a run.
    """
    findings: list = []
    run: list = []
    start = 0
    preamble = directives
    before_body = True
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if preamble:
            if PARSER_DIRECTIVE.match(stripped):
                continue
            preamble = False
        is_comment = bool(stripped) and stripped.startswith(markers)
        # A shebang is a shebang while only blank lines or jinja statements
        # (`{% %}`, the secrets seam a *.sh.jinja opens with) precede it.
        if before_body and stripped.startswith("#!"):
            is_comment = False
        if stripped and not JINJA_TAG_LINE.match(stripped):
            before_body = False
        if is_comment:
            if not run:
                start = number
            run.append(_strip_markers(stripped, markers))
            continue
        if run:
            _record(findings, path, start, run, limits)
            run = []
    if run:
        _record(findings, path, start, run, limits)
    return findings


def _in_string(line: str, index: int) -> bool:
    """Whether `index` sits inside a double-quoted string on this line."""
    quotes = 0
    escaped = False
    for char in line[:index]:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == '"':
            quotes += 1
    return quotes % 2 == 1


def _opener_at(line: str, opener: str) -> int:
    """Index of `opener` in `line`, or -1.

    A `{#` after `$` is bash's `${#array[@]}`, and a `/*` inside a quoted string
    is a URL or ARN glob; either taken as a comment swallows the file.
    """
    start = 0
    while True:
        found = line.find(opener, start)
        if found < 0:
            return -1
        jinja_array = opener == "{#" and found and line[found - 1] == "$"
        quoted_glob = opener == "/*" and _in_string(line, found)
        if not (jinja_array or quoted_glob):
            return found
        start = found + 1


def block_comment_findings(
    path: Path, text: str, delimiters: tuple, limits: Limits
) -> list:
    """Delimited comment blocks (`/* */`, `{# #}`) longer than their limit."""
    findings: list = []
    lines = text.splitlines()
    for opener, closer in delimiters:
        number = 0
        while number < len(lines):
            opens_at = _opener_at(lines[number], opener)
            if opens_at < 0:
                number += 1
                continue
            start = number
            body = [lines[number][opens_at + len(opener):]]
            # A second opener before the closer means the first was text, not a
            # comment: scanning past it would report one block spanning both.
            while closer not in body[-1] and number + 1 < len(lines):
                if _opener_at(lines[number + 1], opener) >= 0:
                    break
                number += 1
                body.append(lines[number])
            end = number
            if closer not in body[-1]:
                # No closer in reach: a `/*` inside a string, or an unterminated
                # block the language's own tooling reports.
                number = start + 1
                continue
            body = [ln.replace(closer, "").strip().lstrip("*").strip() for ln in body]
            _record(findings, path, start + 1, body, limits)
            number = end + 1
    return findings


def _newlines(text: str) -> str:
    """Only the line breaks `text` spanned, so a line number still holds."""
    return "\n" * text.count("\n")


def _neutralize_tags(text: str) -> str:
    """One stretch of template with its jinja tags replaced.

    A statement and a comment leave their line breaks; a `{{ }}` expression
    also leaves a `_` name, because the code around it needs one.
    """
    def newlines(match) -> str:
        return _newlines(match.group(0))

    def name(match) -> str:
        return "_" + newlines(match)

    text = JINJA_COMMENT_TAG.sub(newlines, text)
    text = JINJA_STATEMENT.sub(newlines, text)
    return JINJA_EXPRESSION.sub(name, text)


def _neutralize_jinja(text: str) -> str:
    """A template with its jinja tags replaced so it parses as Python.

    A `{% raw %}` body is kept verbatim, only its tags dropped: jinja reads no
    tags inside it, so its `{{` belongs to the Python it renders.
    """
    pieces: list = []
    cursor = 0
    for raw in JINJA_RAW.finditer(text):
        pieces.append(_neutralize_tags(text[cursor:raw.start()]))
        pieces.append(
            _newlines(raw.group("open"))
            + raw.group("body")
            + _newlines(raw.group("close"))
        )
        cursor = raw.end()
    pieces.append(_neutralize_tags(text[cursor:]))
    return "".join(pieces)


def parse_python(text: str, template: bool = False):
    """The AST of Python source, or None when it does not parse.

    A template source is retried with its jinja neutralized, so a
    `main.py.jinja` has its docstrings checked like the file it renders into.
    """
    try:
        return ast.parse(text)
    except SyntaxError:
        if not template:
            return None
    try:
        return ast.parse(_neutralize_jinja(text))
    except SyntaxError:
        return None


def docstring_findings(
    path: Path,
    text: str,
    limits: Limits,
    unparsed: list | None = None,
    template: bool = False,
) -> list:
    """Python module, class and function docstrings longer than their limit."""
    tree = parse_python(text, template)
    if tree is None:
        if unparsed is not None:
            unparsed.append(path)
        return []
    findings: list = []
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        body = [ln.strip() for ln in first.value.value.strip().splitlines()]
        _record(findings, path, first.value.lineno, body, limits)
    return findings


def _shift(findings: list, offset: int) -> list:
    """The same findings, numbered against the enclosing document."""
    return [
        Finding(f.path, f.line + offset, f.length, f.limit, f.first) for f in findings
    ]


def fenced_blocks(text: str) -> list:
    """(language, body, first body line number) per fenced code block."""
    blocks: list = []
    lines = text.splitlines()
    number = 0
    while number < len(lines):
        opener = FENCE.match(lines[number])
        if not opener:
            number += 1
            continue
        marker, language = opener.group(1), opener.group(2).lower()
        start = number + 1
        number = start
        while number < len(lines):
            closer = FENCE.match(lines[number])
            # The closer repeats the opener's character, at least as long.
            if closer and closer.group(1)[0] == marker[0] and len(closer.group(1)) >= len(marker):
                break
            number += 1
        blocks.append((language, lines[start:number], start))
        number += 1
    return blocks


def fenced_findings(path: Path, text: str, limits: Limits) -> list:
    """Over-long comment blocks inside the fenced code of a Markdown document."""
    findings: list = []
    for language, body, start in fenced_blocks(text):
        suffix = FENCE_LANGUAGES.get(language)
        if not suffix:
            continue
        markers = LINE_MARKERS.get(suffix) or NAME_MARKERS.get(suffix)
        delimiters = BLOCK_DELIMITERS.get(suffix) or ()
        if not markers and not delimiters:
            continue
        snippet = "\n".join(body)
        if markers:
            findings += _shift(
                line_comment_findings(path, snippet, markers, limits), start
            )
        if delimiters:
            findings += _shift(
                block_comment_findings(path, snippet, delimiters, limits), start
            )
    return findings


def is_markdown(path: Path) -> bool:
    """Whether the path renders into a Markdown document."""
    return suffix_for(path).lower() in MARKDOWN_SUFFIXES


def check_file(
    path: Path,
    limits: Limits,
    unreadable: list | None = None,
    unparsed: list | None = None,
    fenced: bool = False,
) -> list:
    """Every over-long comment block in one file."""
    suffix = suffix_for(path)
    markers = markers_for(path)
    delimiters = BLOCK_DELIMITERS.get(suffix) or ()
    template = path.name.endswith(TEMPLATE_SUFFIX)
    if template:
        delimiters = tuple(dict.fromkeys(delimiters + JINJA_BLOCK))
    scan_fences = fenced and is_markdown(path)
    if not markers and not delimiters and not scan_fences:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        if unreadable is not None:
            unreadable.append(path)
        return []
    findings: list = []
    if scan_fences:
        findings.extend(fenced_findings(path, text, limits))
    if markers:
        findings.extend(
            line_comment_findings(
                path, text, markers, limits, takes_parser_directives(path)
            )
        )
    if delimiters:
        findings.extend(block_comment_findings(path, text, delimiters, limits))
    if suffix == ".py":
        findings.extend(
            docstring_findings(path, text, limits, unparsed, template)
        )
    return sorted(findings, key=lambda f: (str(f.path), f.line))


def load_config(path: Path) -> dict:
    """The include/exclude globs and limits from a YAML (or JSON) config file."""
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml

        data = yaml.safe_load(raw)
    except ImportError:
        data = json.loads(raw)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("config must be a mapping")
    for key in ("paths", "include", "exclude"):
        if key in data and not isinstance(data[key], (list, tuple)):
            raise ValueError(f"{key} must be a list of globs")
    return data


def matches(path: Path, patterns: tuple) -> bool:
    """Whether `path` matches any pattern, as written or as `*/pattern`."""
    text = str(path)
    return any(
        fnmatch.fnmatch(text, pattern) or fnmatch.fnmatch(text, f"*/{pattern}")
        for pattern in patterns
    )


def collect(roots: list, include: tuple, exclude: tuple, fenced: bool = False) -> list:
    """Every candidate file under `roots`, filtered by the glob sets."""
    found: list = []
    seen: set = set()
    for root in roots:
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            scannable = (
                markers_for(candidate)
                or suffix_for(candidate) in BLOCK_DELIMITERS
                or (fenced and is_markdown(candidate))
            )
            if not scannable:
                continue
            if exclude and matches(candidate, exclude):
                continue
            if include and not matches(candidate, include):
                continue
            key = candidate.resolve()
            if key in seen:
                continue
            seen.add(key)
            found.append(candidate)
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail a comment block over three content lines.",
        epilog=(
            "Blank lines and comment markers do not count, and a blank line is "
            "what ends a block: a bare '#' keeps the same block going. A block "
            "whose first line starts with 'CRITICAL:' may run to eight. A "
            "Dockerfile's leading '# syntax=' / '# escape=' parser directives "
            "are not comments. A .py.jinja template has its jinja tags "
            "neutralized before it is parsed, and one that still does not "
            "parse is a warning unless --strict-jinja. Markdown is scanned "
            "only with --scan-fenced-code, "
            "which reads the comments inside its fenced snippets. Config keys: "
            "paths, include, exclude, max_lines, critical_max_lines, scan_fenced_code."
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories to scan")
    parser.add_argument("--config", type=Path, help="YAML config with include/exclude globs")
    parser.add_argument("--include", action="append", default=[], help="glob to include")
    parser.add_argument("--exclude", action="append", default=[], help="glob to exclude")
    parser.add_argument("--max", type=int, help=f"lines per block (default {MAX_LINES})")
    parser.add_argument(
        "--critical-max",
        type=int,
        help=f"lines for a CRITICAL: block (default {CRITICAL_MAX_LINES})",
    )
    parser.add_argument(
        "--strict-jinja",
        action="store_true",
        help="exit 2 when a .jinja template source still does not parse as "
             "Python once its jinja tags are neutralized, rather than warning",
    )
    parser.add_argument(
        "--scan-fenced-code",
        action="store_true",
        default=None,
        help="also apply the limit inside Markdown fenced code blocks, so a "
             "wiring snippet people copy into a real file is held to it",
    )
    args = parser.parse_args(argv)

    config: dict = {}
    if args.config:
        if not args.config.is_file():
            print(f"ERROR: config not found: {args.config}", file=sys.stderr)
            return 2
        try:
            config = load_config(args.config)
        except Exception as exc:
            print(f"ERROR: {args.config}: {exc}", file=sys.stderr)
            return 2

    max_lines = args.max if args.max is not None else config.get("max_lines", MAX_LINES)
    critical_max = (
        args.critical_max if args.critical_max is not None
        else config.get("critical_max_lines", CRITICAL_MAX_LINES)
    )
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (max_lines, critical_max)
    ):
        print("ERROR: max_lines and critical_max_lines must be integers", file=sys.stderr)
        return 2
    limits = Limits(max_lines, critical_max)
    if limits.plain < 1 or limits.critical < limits.plain:
        print("ERROR: max_lines must be >= 1 and <= critical_max_lines", file=sys.stderr)
        return 2

    fenced = (
        args.scan_fenced_code if args.scan_fenced_code is not None
        else bool(config.get("scan_fenced_code", False))
    )
    include = tuple(args.include) + tuple(config.get("include", ()))
    exclude = tuple(args.exclude) + tuple(config.get("exclude", ())) + DEFAULT_EXCLUDES
    roots = args.paths or [Path(p) for p in config.get("paths", ())] or [Path(".")]
    for root in roots:
        if not root.exists():
            print(f"ERROR: path not found: {root}", file=sys.stderr)
            return 2

    files = collect(roots, include, exclude, fenced)
    if not files:
        print("ERROR: no scannable files found", file=sys.stderr)
        return 2

    findings: list = []
    unreadable: list = []
    unparsed: list = []
    for path in files:
        findings.extend(check_file(path, limits, unreadable, unparsed, fenced))

    if unreadable:
        print(f"ERROR: {len(unreadable)} file(s) could not be decoded and were "
              "NOT checked:", file=sys.stderr)
        for path in unreadable:
            print(f"  {path}", file=sys.stderr)
        return 2

    templates = [] if args.strict_jinja else [
        path for path in unparsed if path.name.endswith(TEMPLATE_SUFFIX)
    ]
    if templates:
        # A template the gate cannot parse is the template's problem, not the
        # consumer's pipeline: its comment blocks are still checked.
        names = ", ".join(str(path) for path in templates)
        print(f"WARNING: {len(templates)} jinja template(s) do not parse as "
              f"Python even with their tags neutralized; their docstrings were "
              f"NOT checked: {names}", file=sys.stderr)

    unparsable = [path for path in unparsed if path not in templates]
    if unparsable:
        print(f"ERROR: {len(unparsable)} file(s) could not be parsed as Python; "
              "their docstrings were NOT checked:", file=sys.stderr)
        for path in unparsable:
            print(f"  {path}", file=sys.stderr)
        return 2

    if findings:
        print(f"ERROR: {len(findings)} comment block(s) over the line limit:")
        for finding in findings:
            print(f"  {finding}")
        print("  Trim it, or open the block with 'CRITICAL:' when it guards an outage.")
        return 1

    print(f"OK: {len(files)} file(s) scanned, every comment block within its limit.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
