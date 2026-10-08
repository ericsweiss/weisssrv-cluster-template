#!/usr/bin/env python3
"""Assert every CI deploy job's playbook resolves and would actually do work.

Runs `--list-hosts` per call under the job's `--limit`, and `--list-tasks` per
`--tags` selection: a selection matching nothing makes the job a green no-op.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from ci_playbook_invocations import parse_invocations  # noqa: E402
    from ci_yaml import load_ci, script_lines  # noqa: E402
except ImportError as exc:
    print(
        f"ERROR: {exc.name or 'the companion module'}.py must sit next to this "
        "script — re-vendor it from weisssrv-lib.",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

try:
    import yaml  # noqa: E402
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

ANSIBLE_DIR = Path("ansible")
CI_FILE = Path(".gitlab-ci.yml")
# An unreadable pipeline is an input error, not a finding: exit 2 so it cannot
# read as a clean gate.
try:
    pipeline = load_ci(CI_FILE)
except (OSError, yaml.YAMLError) as exc:
    print(f"ERROR: {CI_FILE} could not be read, so no deploy job was inspected: {exc}",
          file=sys.stderr)
    raise SystemExit(2) from None

# `--list-tasks` prints one `TAGS: [a, b]` per selected task. `always` tasks
# are printed for ANY selection, so output alone proves nothing — the
# requested tag must appear in a task's own tag list.
TASK_TAGS = re.compile(r"TAGS: \[([^\]]*)\]")
# `--list-hosts` prints one `hosts (N):` per play. All zero means the limit and
# the plays' own `hosts:` patterns do not intersect.
PLAY_HOSTS = re.compile(r"hosts \((\d+)\):")

# Every check below shells out to it, so without it the gate inspects nothing.
if shutil.which("ansible-playbook") is None:
    print("ERROR: ansible-playbook is not on PATH, so no deploy job was "
          "inspected.", file=sys.stderr)
    raise SystemExit(2)


def ansible(*argv: str) -> subprocess.CompletedProcess:
    """`ansible-playbook` under ANSIBLE_DIR. A failure to execute it at all is an
    operator error, so it exits 2 rather than reporting a finding."""
    try:
        return subprocess.run(
            ["ansible-playbook", *argv], cwd=ANSIBLE_DIR,
            capture_output=True, text=True,
        )
    except OSError as exc:
        print(f"ERROR: could not run ansible-playbook: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

failures = []
playbooks = set()
selections = 0

for name, job in pipeline.items():
    if not isinstance(job, dict) or name.startswith("."):
        continue
    # extends is a string OR a list; a job naming several parents must not
    # slip the preflight.
    parents = job.get("extends") or []
    if isinstance(parents, str):
        parents = [parents]
    if not {".deploy-base", ".maintenance-base"}.intersection(parents):
        continue
    raw = job.get("script")
    if isinstance(raw, str):
        raw = [raw]
    # An inherited script is absent here and a reference into an included file
    # cannot be followed; either way the checks below would be vacuous.
    unresolved = []
    lines = script_lines(job, pipeline, unresolved=unresolved) if isinstance(raw, list) else []
    if not isinstance(raw, list) or unresolved or not lines:
        paths = ", ".join(" ".join(str(key) for key in path) for path in unresolved)
        detail = (
            f"its `!reference` to {paths} resolves to nothing in this file"
            if unresolved else "it carries no literal commands"
        )
        failures.append(
            f"{name}: script: was not inspected because {detail} - an inherited "
            "script or a reference into an included file cannot be followed. "
            "Inline the ansible-playbook invocation."
        )
        continue
    script = "\n".join(lines)
    parsed = parse_invocations(script)
    # Refuse to shrink silently: a shape the parser cannot read would cost
    # coverage with no output at all.
    written = script.count("ansible-playbook")
    if len(parsed) < written:
        failures.append(
            f"{name}: parsed {len(parsed)} of {written} `ansible-playbook` "
            "invocation(s) - this job is written in a shape the preflight "
            "parser does not understand, so its playbook/tag checks were "
            "skipped. Fix ci_playbook_invocations.py rather than the job."
        )
    for call in parsed:
        playbook = call["playbook"]
        limit = call["limit"]
        if not (ANSIBLE_DIR / playbook).is_file():
            failures.append(f"{name}: playbook ansible/{playbook} does not exist")
            continue
        playbooks.add(playbook)
        # `--skip-tags` excludes rather than selects, so it is not a selection:
        # an inert value there is not a no-op step.
        tags = sorted(call["tags"] or ())
        # A job may omit `-i` and take ansible.cfg's default inventory.
        # Omitting the flag reproduces that; `-i None` is a TypeError.
        inventory_argv = ["-i", call["inventory"]] if call["inventory"] else []
        limit_argv = ["--limit", limit] if limit else []
        scope = f"--limit {limit}" if limit else "its own hosts: patterns"
        hosts = ansible(*inventory_argv, playbook, *limit_argv, "--list-hosts")
        counts = [int(n) for n in PLAY_HOSTS.findall(hosts.stdout)]
        if hosts.returncode != 0:
            failures.append(
                f"{name}: --list-hosts failed for {playbook} ({scope})\n"
                f"{hosts.stderr.strip()}"
            )
        elif not counts:
            failures.append(
                f"{name}: --list-hosts for {playbook} ({scope}) printed no "
                "`hosts (N):` line - the preflight could not read the play "
                "list, so the host check was skipped. Fix PLAY_HOSTS rather "
                "than the job."
            )
        elif not any(counts):
            failures.append(
                f"{name}: {scope} on {playbook} matches NO host in any play - "
                "that deploy step is a silent no-op"
            )
        for tag in tags:
            selections += 1
            proc = ansible(*inventory_argv, playbook, *limit_argv,
                           "--list-tasks", "--tags", tag)
            if proc.returncode != 0:
                failures.append(
                    f"{name}: --list-tasks failed for {playbook} --tags {tag}\n"
                    f"{proc.stderr.strip()}"
                )
                continue
            selected = any(
                tag in [t.strip() for t in m.group(1).split(",")]
                for m in TASK_TAGS.finditer(proc.stdout)
            )
            if selected:
                print(f"OK   {name}: {playbook} --tags {tag}")
            else:
                failures.append(
                    f"{name}: `--tags {tag}` on {playbook} selects NO task - "
                    "that deploy step is a silent no-op"
                )

if not playbooks:
    failures.append(
        "preflight resolved 0 playbooks - candidate-job selection or "
        "invocation parsing is no longer inspecting the deploy jobs"
    )

# The starter deploy jobs run whole playbooks, so 0 tag selections is the
# expected state, not a fault - it only means the no-op-tag guard had
# nothing to inspect.
print(f"\n{len(playbooks)} playbook(s), {selections} tag selection(s) checked")
if failures:
    print("", file=sys.stderr)
    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    sys.exit(1)
