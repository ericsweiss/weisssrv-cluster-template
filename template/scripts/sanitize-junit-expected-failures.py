#!/usr/bin/env python3
"""sanitize-junit-expected-failures.py - Downgrade DECLARED negative-path junit failures.

Several molecule scenarios deliberately drive role guard tasks to failure
(inside block/rescue, or via failed_when exercises) to prove the guard fires.
The ansible junit callback records the RAW task failure even though the rescue
handles it and the scenario's assertions pass — so a fully green job still
shows red testcases in GitLab's pipeline test report.

This tool rewrites junit XML files: a <testcase> carrying <failure>/<error>
whose name contains a substring DECLARED by the scenario (in an
`expected-junit-failures.txt` next to its molecule.yml) has the failure element
replaced with a <system-out> note, so the test report matches job reality.

Safety properties:
  * Only DECLARED names are touched — an undeclared failure stays red in the
    report (and is printed as a warning here).
  * CI runs this only after molecule SUCCEEDS (last script step): a red job
    keeps its raw failure report untouched.
  * The declaration file is colocated with the scenario that creates the
    expected failures, so the expectation is reviewed with the test.

Declaration file format: one case-sensitive substring per line, matched against
the junit testcase name; blank lines and `#` comments ignored.

Usage:
    sanitize-junit-expected-failures.py --junit-dir junit \\
        --expectations ansible/roles/<role>/molecule/<scenario>/expected-junit-failures.txt
A missing/absent expectations file is a no-op (exit 0) so callers can pass the
path unconditionally.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def load_expectations(path: Path) -> list[str]:
    """Substring patterns from a declaration file; [] when the file is absent."""
    if not path.is_file():
        return []
    patterns = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def sanitize_file(xml_path: Path, patterns: list[str]) -> tuple[int, list[str]]:
    """Rewrite one junit XML; return (downgraded_count, undeclared_failure_names)."""
    # Entity-attack guard without a defusedxml dependency: stdlib ElementTree
    # only expands entities declared in a DTD, and the ansible junit callback
    # never writes one — so any DOCTYPE here is not our callback's output.
    # Refuse it rather than parse it.
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
        if any(p in name for p in patterns):
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit-dir", required=True, type=Path)
    parser.add_argument("--expectations", required=True, type=Path)
    args = parser.parse_args(argv)

    patterns = load_expectations(args.expectations)
    if not patterns:
        print(f"no expectations declared ({args.expectations}); junit left untouched")
        return 0
    if not args.junit_dir.is_dir():
        print(f"junit dir {args.junit_dir} absent; nothing to sanitize")
        return 0

    total = 0
    for xml_path in sorted(args.junit_dir.glob("*.xml")):
        downgraded, undeclared = sanitize_file(xml_path, patterns)
        total += downgraded
        for name in undeclared:
            print(f"WARNING: undeclared junit failure left red in {xml_path.name}: {name[:160]}")
    print(f"downgraded {total} declared negative-path failure(s) across {args.junit_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
