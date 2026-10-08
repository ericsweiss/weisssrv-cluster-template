"""Render this template into a throwaway directory.

Shared by the pytest suite and tests/validate_render.py. The source is copied to
a scratch directory with .git left behind, so copier renders the working tree.
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
    """The jobs document of a pipeline file.

    GitLab's inputs syntax makes a pipeline two documents, `spec:` then the jobs,
    so the last mapping document is the one a caller wants.
    """
    docs = [d for d in yaml.load_all(path.read_text(), Loader=CILoader) if isinstance(d, dict)]
    return docs[-1] if docs else {}

# `.tmp` carries this pipeline's two weisssrv-lib clones, so copying it would
# duplicate hundreds of megabytes into every render.
_IGNORED = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".render", ".tmp", ".bin"
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
