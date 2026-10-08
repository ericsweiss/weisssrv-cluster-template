#!/usr/bin/env python3
"""Assert the integration-test tree and the CI `parallel:matrix` cover each other.

A suite with no matrix entry never runs; an entry naming no directory fails in
the DinD fan-out. Exit 0 clean, 1 a mismatch, 2 nothing inspected.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

try:
    from ci_yaml import load_ci  # noqa: E402
except ImportError as exc:
    print(
        f"ERROR: {exc.name or 'the companion module'}.py must sit next to this "
        "script — re-vendor it from weisssrv-lib.",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent

DEFAULT_CI_FILE = ".gitlab/ci/integration-jobs.yml"
DEFAULT_INTEGRATION_DIR = "ansible/integration-tests"
DEFAULT_INTEGRATION_JOB = "integration-tests"


def matrix_tests(job: dict) -> set[str]:
    """Every TEST value across the job's `parallel:matrix` entries."""
    names: set[str] = set()
    for entry in (job.get("parallel") or {}).get("matrix") or []:
        if not isinstance(entry, dict):
            continue
        tests = entry.get("TEST")
        if isinstance(tests, list):
            names.update(t for t in tests if isinstance(t, str))
        elif isinstance(tests, str):
            names.add(tests)
    return names


def suite_dirs(root: Path) -> set[str]:
    """Directory names holding at least one molecule/<scenario>/molecule.yml.

    The job does `cd <dir>/$TEST && molecule test`, so the directory name is the
    matrix identifier.
    """
    return {
        d.name
        for d in root.iterdir()
        if d.is_dir() and any((d / "molecule").glob("*/molecule.yml"))
    }


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ci-file", default=DEFAULT_CI_FILE,
                        help=f"pipeline file holding the job (default: {DEFAULT_CI_FILE})")
    parser.add_argument("--integration-dir", default=DEFAULT_INTEGRATION_DIR,
                        help="suite tree; an empty value declares no integration "
                             f"suite (default: {DEFAULT_INTEGRATION_DIR})")
    parser.add_argument("--integration-job", default=DEFAULT_INTEGRATION_JOB,
                        help=f"job name to read (default: {DEFAULT_INTEGRATION_JOB})")
    opts = parser.parse_args()

    if opts.integration_dir == "":
        print('No integration suite declared (--integration-dir "") — matrix '
              "comparison skipped.")
        return 0

    ci_file = _resolve(opts.ci_file)
    try:
        ci = load_ci(ci_file)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        print(f"ERROR: cannot read {opts.ci_file}: {exc}", file=sys.stderr)
        return 2
    job = ci.get(opts.integration_job)
    if not isinstance(job, dict):
        print(f"ERROR: {opts.ci_file} has no job {opts.integration_job!r}", file=sys.stderr)
        return 2
    in_ci = matrix_tests(job)

    it_dir = _resolve(opts.integration_dir)
    if not it_dir.is_dir():
        print(f"ERROR: integration directory {opts.integration_dir!r} does not exist",
              file=sys.stderr)
        return 2
    on_disk = suite_dirs(it_dir)

    # Both sides empty makes every comparison below trivially clean, so the gate
    # would report full coverage having inspected no suite.
    if not on_disk and not in_ci:
        print(
            f"ERROR: the gate inspected nothing — no molecule.yml under "
            f"{opts.integration_dir}/ and no {opts.integration_job} matrix entry "
            f"in {opts.ci_file}.\n  Point the gate at the real tree and job name, "
            'or declare the suite absent with --integration-dir "".',
            file=sys.stderr,
        )
        return 2

    missing = sorted(on_disk - in_ci)
    stale = sorted(in_ci - on_disk)
    if missing:
        print(f"ERROR: integration test(s) with no {opts.integration_job} matrix "
              "entry:\n", file=sys.stderr)
        for name in missing:
            print(f"  - {opts.integration_dir}/{name}/", file=sys.stderr)
        print(f"\n  Add the name to the TEST list in {opts.ci_file}.\n", file=sys.stderr)
    if stale:
        print(f"ERROR: {opts.integration_job} matrix entries naming no directory:\n",
              file=sys.stderr)
        for name in stale:
            print(f"  - {name} (no {opts.integration_dir}/{name}/)", file=sys.stderr)
        print(f"\n  Drop the entry from the TEST list in {opts.ci_file}.\n",
              file=sys.stderr)
    if missing or stale:
        return 1

    print(f"Integration matrix covers all {len(on_disk)} test dir(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
