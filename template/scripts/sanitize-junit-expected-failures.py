#!/usr/bin/env python3
"""Downgrade junit failures a molecule scenario DECLARED it drives on purpose.

Rewrites the junit XML in --junit-dir; an undeclared failure stays red and
--strict fails a stale declaration. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def load_expectations(path: Path) -> list[tuple[str, int]]:
    """(substring pattern, testcases it matches) pairs; [] when absent.

    A trailing ` ::<n>` declares the count; a plain line declares one.
    """
    if not path.is_file():
        return []
    expectations: list[tuple[str, int]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pattern, separator, count = line.rpartition(" ::")
        if separator and pattern.strip() and count.strip().isdigit() and int(count) > 0:
            expectations.append((pattern.strip(), int(count)))
        else:
            expectations.append((line, 1))
    return expectations


def sanitize_file(
    xml_path: Path, patterns: list[str], counts: dict[str, int] | None = None
) -> tuple[int, list[str]]:
    """Rewrite one junit XML; return (downgraded_count, undeclared_failure_names).

    `counts`, when given, accumulates how many testcases each pattern hit, so a
    caller can tell a stale declaration from an over-broad one.
    """
    # Entity-attack guard without a defusedxml dependency: stdlib ElementTree
    # only expands entities declared in a DTD, and the ansible junit callback
    # never writes one, so any DOCTYPE here is refused rather than parsed.
    head = xml_path.read_text(errors="replace")
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise ValueError(f"{xml_path}: contains a DTD/entity declaration — refusing to parse")
    tree = ET.parse(xml_path)
    root = tree.getroot()
    downgraded = 0
    undeclared: list[str] = []
    for testcase in root.iter("testcase"):
        blockers = [el for el in list(testcase) if el.tag in ("failure", "error")]
        if not blockers:
            continue
        name = testcase.get("name", "")
        hits = [p for p in patterns if p in name]
        if hits:
            if counts is not None:
                for hit in hits:
                    counts[hit] = counts.get(hit, 0) + 1
            for el in blockers:
                testcase.remove(el)
            note = ET.SubElement(testcase, "system-out")
            note.text = (
                "expected negative-path failure (declared in the scenario's "
                "expected-junit-failures.txt); the rescue/assertions passed — "
                "job status is the arbiter"
            )
            downgraded += 1
        else:
            undeclared.append(name)
    if downgraded:
        # Keep suite counters consistent with the rewritten cases — including
        # the AGGREGATE attributes on a <testsuites> root, which some
        # consumers read in preference to per-suite counts.
        elements = [root] if root.tag == "testsuite" else list(root.iter("testsuite")) + [root]
        for suite in elements:
            failures = sum(
                1 for tc in suite.iter("testcase")
                for el in list(tc) if el.tag == "failure"
            )
            errors = sum(
                1 for tc in suite.iter("testcase")
                for el in list(tc) if el.tag == "error"
            )
            if suite.get("failures") is not None:
                suite.set("failures", str(failures))
            if suite.get("errors") is not None:
                suite.set("errors", str(errors))
        tree.write(xml_path, encoding="unicode", xml_declaration=True)
    return downgraded, undeclared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "Declaration file: one case-sensitive substring per line matched "
            "against the junit testcase name, optionally followed by ` ::<n>` "
            "for the number of testcases it matches (default 1); blank lines "
            "and # comments are ignored, and an absent file is a no-op so "
            "callers can pass the path unconditionally."
        ),
    )
    parser.add_argument("--junit-dir", required=True, type=Path)
    parser.add_argument("--expectations", required=True, type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when a declared expectation matched no testcase, matched a "
             "different number of testcases than it declares, or could not be "
             "observed at all",
    )
    args = parser.parse_args(argv)

    expectations = load_expectations(args.expectations)
    if not expectations:
        print(f"no expectations declared ({args.expectations}); junit left untouched")
        return 0
    patterns = [pattern for pattern, _ in expectations]
    if not args.junit_dir.is_dir():
        message = (
            f"{args.expectations} declares {len(patterns)} expectation(s) but "
            f"{args.junit_dir} does not exist, so none could be observed"
        )
        print(message, file=sys.stderr if args.strict else sys.stdout)
        return 1 if args.strict else 0

    total = 0
    counts: dict[str, int] = {}
    for xml_path in sorted(args.junit_dir.glob("*.xml")):
        downgraded, undeclared = sanitize_file(xml_path, patterns, counts=counts)
        total += downgraded
        for name in undeclared:
            print(f"WARNING: undeclared junit failure left red in {xml_path.name}: {name[:160]}")
    print(f"downgraded {total} declared negative-path failure(s) across {args.junit_dir}")

    unobserved = [p for p in patterns if not counts.get(p)]
    if unobserved and args.strict:
        for pattern in unobserved:
            print(f"ERROR: declared expectation never observed: {pattern}", file=sys.stderr)
        print(
            f"    Fix: the guard was renamed or removed — drop the line from "
            f"{args.expectations}, or restore the negative-path task.",
            file=sys.stderr,
        )
        return 1
    for pattern in unobserved:
        print(f"WARNING: declared expectation never observed: {pattern}")

    # The count is EXACT under --strict, in both directions: a declaration wider
    # than the guard can produce would otherwise absorb a real extra failure of
    # that same guard, and nothing would say so.
    mismatched = [
        (pattern, counts[pattern], declared)
        for pattern, declared in expectations
        if counts.get(pattern, 0) and counts[pattern] != declared
    ]
    if mismatched and args.strict:
        for pattern, observed, declared in mismatched:
            print(
                f"ERROR: declared expectation matched {observed} testcases, "
                f"declared {declared}: {pattern}",
                file=sys.stderr,
            )
        if any(observed > declared for _, observed, declared in mismatched):
            print(
                f"    Fix (matched more): narrow the pattern so it names only "
                f"the negative-path task, or raise the count as "
                f"`<pattern> ::<n>` in {args.expectations}.",
                file=sys.stderr,
            )
        if any(observed < declared for _, observed, declared in mismatched):
            print(
                f"    Fix (matched fewer): lower the count in "
                f"{args.expectations}, or restore the negative-path case that "
                f"stopped firing.",
                file=sys.stderr,
            )
        return 1
    for pattern, observed, declared in mismatched:
        print(
            f"WARNING: declared expectation matched {observed} testcases, "
            f"declared {declared}: {pattern}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
