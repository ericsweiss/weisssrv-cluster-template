#!/usr/bin/env python3
"""Prove every deploy job's `ansible-playbook` call would actually do work.

Runs `--list-tasks` per `--tags` selection and `--list-hosts` per call, under the
job's own `--limit` and `--skip-tags`. Usage: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from ci_playbook_invocations import parse_invocations  # noqa: E402
    from ci_yaml import jobs, load_ci, script_lines  # noqa: E402
except ImportError as exc:
    print(
        f"ERROR: {exc.name or 'the companion module'}.py must sit next to this "
        "script — vendor it (see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

# `--list-hosts` prints one `hosts (N):` per play. All zero means the limit and
# the plays' own `hosts:` patterns do not intersect.
PLAY_HOSTS = re.compile(r"hosts \((\d+)\):")
# `--list-tasks` prints one `TAGS: [a, b]` per selected task. `always` tasks are
# printed for ANY selection, so output alone proves nothing: the requested tag
# must appear in a task's own tag list.
TASK_TAGS = re.compile(r"TAGS: \[([^\]]*)\]")


def _candidates(doc: dict, extends: List[str]) -> List[Tuple[str, dict]]:
    wanted = set(extends)
    out = []
    for name, job in jobs(doc).items():
        # `extends` is a string OR a list; a job naming several parents must not
        # slip the preflight.
        parents = job.get("extends") or []
        if isinstance(parents, str):
            parents = [parents]
        if wanted.intersection(parents):
            out.append((name, job))
    return out


def _parents_of(job: dict) -> List[str]:
    """A job's `extends` as a list. GitLab accepts a string or a list, and a
    later entry overrides an earlier one, so the list is walked in reverse."""
    parents = job.get("extends") or []
    return [parents] if isinstance(parents, str) else list(parents)


def _effective_script(job: dict, doc: dict,
                      unresolved: List[list]) -> Tuple[List[str], List[str]]:
    """(script lines, the `extends` chain walked to find them).

    A job declaring no script of its own inherits one; without following the
    chain the preflight reads an empty script and silently checks nothing.
    """
    chain: List[str] = []
    seen: set = set()
    current = job
    while True:
        lines = script_lines(current, doc, unresolved=unresolved)
        if lines:
            return lines, chain
        found = None
        for parent in reversed(_parents_of(current)):
            if parent in seen:
                continue
            seen.add(parent)
            chain.append(parent)
            body = doc.get(parent)
            if isinstance(body, dict) and found is None:
                found = body
        if found is None:
            return [], chain
        current = found


def check(ci_file: Path, ansible_dir: Path, extends: List[str],
          require_tag_selections: bool) -> List[str]:
    doc = load_ci(ci_file)
    failures: List[str] = []
    playbooks = set()
    selections = 0

    for name, job in _candidates(doc, extends):
        unresolved: List[list] = []
        script_body, chain = _effective_script(job, doc, unresolved)
        script = "\n".join(script_body)
        for path in unresolved:
            failures.append(
                "%s: `!reference %s` resolved to nothing (the target is defined "
                "in an included file), so this job's script was not inspected."
                % (name, path)
            )
        parsed = parse_invocations(script)
        written = script.count("ansible-playbook")
        if not script.strip():
            failures.append(
                "%s: no script to inspect — the job declares none and its "
                "`extends` chain (%s) resolves to none in this file, so its "
                "playbook, tag and limit checks were skipped. Inline the "
                "ansible-playbook invocation in the job, or name the parent "
                "that carries it with --extends."
                % (name, ", ".join(chain) or "empty")
            )
        if len(parsed) < written:
            failures.append(
                "%s: parsed %d of %d `ansible-playbook` invocation(s) — this job "
                "is written in a shape the preflight parser does not understand, "
                "so its playbook/tag checks were skipped. Fix "
                "parse_invocations() "                "rather than the job." % (name, len(parsed), written)
            )
        for call in parsed:
            inventory, playbook = call["inventory"], call["playbook"]
            limit = call["limit"]
            if not (ansible_dir / playbook).is_file():
                failures.append(
                    "%s: playbook %s/%s does not exist" % (name, ansible_dir, playbook)
                )
                continue
            playbooks.add(playbook)
            tags = sorted(call["tags"] or ())
            inventory_argv = ["-i", inventory] if inventory else []
            limit_argv = ["--limit", limit] if limit else []
            # The job's own skip list, so a tag whose every task the job skips
            # is reported as the no-op it is rather than scored as selected.
            skipped = sorted(call["skip_tags"] or ())
            skip_argv = ["--skip-tags", ",".join(skipped)] if skipped else []
            scope = "--limit %s" % limit if limit else "its own hosts: patterns"
            hosts = subprocess.run(
                ["ansible-playbook", *inventory_argv, playbook,
                 *limit_argv, "--list-hosts"],
                cwd=ansible_dir, capture_output=True, text=True,
            )
            counts = [int(n) for n in PLAY_HOSTS.findall(hosts.stdout)]
            if hosts.returncode != 0:
                failures.append(
                    "%s: --list-hosts failed for %s (%s)\n%s"
                    % (name, playbook, scope, hosts.stderr.strip())
                )
            elif not counts:
                failures.append(
                    "%s: --list-hosts for %s (%s) printed no "
                    "`hosts (N):` line — the preflight could not read the "
                    "play list, so the host check was skipped. Fix "
                    "PLAY_HOSTS rather than the job."
                    % (name, playbook, scope)
                )
            elif not any(counts):
                failures.append(
                    "%s: %s on %s matches NO host in any play — "
                    "that deploy step is a silent no-op"
                    % (name, scope, playbook)
                )
            for tag in tags:
                selections += 1
                proc = subprocess.run(
                    ["ansible-playbook", *inventory_argv, playbook,
                     *limit_argv, *skip_argv, "--list-tasks", "--tags", tag],
                    cwd=ansible_dir, capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    failures.append(
                        "%s: --list-tasks failed for %s --tags %s\n%s"
                        % (name, playbook, tag, proc.stderr.strip())
                    )
                    continue
                selected = any(
                    tag in [t.strip() for t in m.group(1).split(",")]
                    for m in TASK_TAGS.finditer(proc.stdout)
                )
                if selected:
                    print("OK   %s: %s --tags %s" % (name, playbook, tag))
                else:
                    failures.append(
                        "%s: `--tags %s` on %s selects NO task — that deploy step "
                        "is a silent no-op" % (name, tag, playbook)
                    )

    if not playbooks:
        failures.append(
            "preflight resolved 0 playbooks — candidate-job selection or "
            "invocation parsing is no longer inspecting the deploy jobs"
        )
    if require_tag_selections and selections == 0:
        failures.append(
            "preflight resolved 0 tag selections — the silent-no-op tag guard "
            "inspected nothing"
        )
    print("\n%d playbook(s), %d tag selection(s) checked" % (len(playbooks), selections))
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ci-file", type=Path, default=Path(".gitlab-ci.yml"))
    parser.add_argument("--ansible-dir", type=Path, default=Path("ansible"))
    parser.add_argument(
        "--extends", action="append", default=None,
        help="hidden job a deploy job extends; repeatable "
             "(default: .deploy-base .maintenance-base)",
    )
    parser.add_argument(
        "--require-tag-selections", action="store_true",
        help="fail when no job selects any tag, for a consumer whose deploy "
             "jobs are all tag-driven",
    )
    args = parser.parse_args(argv)

    if not args.ci_file.is_file():
        print("no such pipeline file: %s" % args.ci_file, file=sys.stderr)
        return 2
    if not args.ansible_dir.is_dir():
        print("no such ansible directory: %s" % args.ansible_dir, file=sys.stderr)
        return 2
    if shutil.which("ansible-playbook") is None:
        print("ERROR: ansible-playbook is not on PATH, so the preflight could "
              "not run", file=sys.stderr)
        return 2

    try:
        failures = check(
            args.ci_file, args.ansible_dir,
            args.extends or [".deploy-base", ".maintenance-base"],
            args.require_tag_selections,
        )
    except OSError as exc:
        print("ERROR: could not run ansible-playbook on PATH: %s" % exc,
              file=sys.stderr)
        return 2
    if failures:
        print("", file=sys.stderr)
        for failure in failures:
            print("FAIL %s" % failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
