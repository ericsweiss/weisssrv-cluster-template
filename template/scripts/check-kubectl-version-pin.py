#!/usr/bin/env python3
"""Assert the CI kubectl pin stays within +/-1 minor of the cluster's k3s_version.

Reads a CI file and a cluster-versions ConfigMap; exits 0 within skew, 1 on
skew, 2 on an operator error. Contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import re

REPO = Path(__file__).resolve().parent.parent
CI_YAML = REPO / (os.environ.get("CI_FILE") or ".gitlab-ci.yml")
VERSIONS_CM = REPO / "kubernetes/infrastructure/sources/versions-configmap.yaml"

_KUBECTL_RE = re.compile(r"dl\.k8s\.io/release/v(\d+)\.(\d+)\.\d+/bin")
# A consumer that includes ci/deploy/kubectl-setup.yml carries the version as an
# include input and no download URL of its own.
_KUBECTL_INPUT_RE = re.compile(r'^\s*kubectl_version:\s*["\']?v?(\d+)\.(\d+)\.\d+', re.M)
_K3S_RE = re.compile(r"^\s*k3s_version:\s*v?(\d+)\.(\d+)", re.M)


def check(
    ci_text: str,
    cm_text: str,
    ci_path: Path = CI_YAML,
    cm_path: Path = VERSIONS_CM,
) -> tuple[int, str]:
    """Return (exit_code, message).

    0 = pin within the supported +/-1 minor skew, 1 = skew, 2 = neither version
    could be extracted. Both caller-supplied paths are named in the messages.
    """
    # Every pin in the file, so a second job's kubectl cannot drift unchecked.
    pins = sorted({(int(m.group(1)), int(m.group(2)))
                   for rx in (_KUBECTL_RE, _KUBECTL_INPUT_RE)
                   for m in rx.finditer(ci_text)})
    if not pins:
        return 2, (
            f"Could not extract a kubectl pin from {ci_path} "
            "(neither a dl.k8s.io download URL nor a kubectl_version: input)"
        )

    m2 = _K3S_RE.search(cm_text)
    if not m2:
        return 2, f"Could not extract k3s_version from {cm_path}"
    smaj, smin = int(m2.group(1)), int(m2.group(2))

    listed = ", ".join(f"v{maj}.{min_}.x" for maj, min_ in pins)
    prefix = f"CI kubectl pin: {listed} / cluster k3s_version: v{smaj}.{smin}.x\n"
    offenders = [p for p in pins if p[0] != smaj or abs(p[1] - smin) > 1]
    if offenders:
        return 1, prefix + "\n".join(
            f"kubectl pin v{maj}.{min_} is outside Kubernetes' supported +/-1 minor "
            f"skew of the cluster (k3s v{smaj}.{smin}) - bump the kubectl version + "
            f"sha256 in {ci_path}."
            for maj, min_ in offenders
        )
    return 0, prefix + "kubectl pin is within the supported +/-1 minor skew of k3s_version."


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=Path(argv[0]).name,
        description=__doc__,
        epilog=(
            "The defaults assume the conventional layout; pass both paths when "
            "the consumer's layout differs. $CI_FILE (repo-relative or absolute) "
            "retargets the first default. The check itself is forge-neutral: it "
            "regexes either a dl.k8s.io download pin or a kubectl_version: "
            "include input out of whatever text it is given."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "ci_yaml", nargs="?", type=Path, default=CI_YAML,
        help=f"CI file carrying the kubectl pin (default: {CI_YAML})",
    )
    parser.add_argument(
        "configmap", nargs="?", type=Path, default=VERSIONS_CM,
        help=f"versions ConfigMap carrying k3s_version (default: {VERSIONS_CM})",
    )
    return parser.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        ci_text = args.ci_yaml.read_text()
        cm_text = args.configmap.read_text()
    except (OSError, UnicodeDecodeError) as e:
        # An absent/unreadable input is an operator error (wrong path, bad
        # permissions), not a skew finding — exit 2 so CI can tell the two
        # apart, and print one line rather than a traceback.
        print(f"ERROR: could not read input file: {e}", file=sys.stderr)
        return 2
    code, message = check(ci_text, cm_text, args.ci_yaml, args.configmap)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
