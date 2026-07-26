"""Render this template into a throwaway directory.

Shared by the pytest suite and tests/validate_render.py so both exercise the
same invocation.

The template source is COPIED to a scratch directory first, with .git left
behind: copier treats a git checkout as a VCS source and renders its committed
HEAD, which would silently test the last commit instead of the working tree
every reviewer and CI job is actually looking at.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSWERS = REPO_ROOT / "tests" / "answers-weisssrv-shaped.yml"


class CILoader(yaml.SafeLoader):
    """GitLab CI YAML carries `!reference [...]`, which SafeLoader rejects.

    A SafeLoader subclass: the added constructor degrades ANY unknown tag to its
    plain sequence value (or None), so no object is ever constructed.
    """


CILoader.add_multi_constructor(
    "",
    lambda loader, suffix, node: (
        loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.SequenceNode)
        else None
    ),
)


def load_ci(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=CILoader)

_IGNORED = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".pytest_cache", ".render", ".bin"
)


def copy_source(scratch: Path) -> Path:
    src = scratch / "template-src"
    shutil.copytree(REPO_ROOT, src, ignore=_IGNORED)
    return src


def render(scratch: Path, answers: Path = ANSWERS, dest_name: str = "render") -> Path:
    """Render the working tree with `answers`; return the generated repo root."""
    src = copy_source(scratch)
    dest = scratch / dest_name
    subprocess.run(
        [
            sys.executable,
            "-m",
            "copier",
            "copy",
            "--defaults",
            "--overwrite",
            "--trust",
            "--data-file",
            str(answers),
            str(src),
            str(dest),
        ],
        check=True,
    )
    return dest


def main() -> int:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description="Render the template for inspection.")
    parser.add_argument("--out", type=Path, help="Directory to render into (must not exist).")
    parser.add_argument("--answers", type=Path, default=ANSWERS)
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="cluster-template-"))
    dest = render(scratch, answers=args.answers)
    if args.out:
        shutil.copytree(dest, args.out)
        shutil.rmtree(scratch, ignore_errors=True)
        dest = args.out
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
