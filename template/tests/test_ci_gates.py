"""Two pipeline-shape gates a YAML lint cannot see.

The python-tests job's hand-kept `changes:` must cover every file whose only gate
is that suite, and every live-estate job must install the tools its scripts call.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml
from conftest import (
    REPO,
    SCRIPTS,
    lib_file,
    load_script,
    pinned_lib_ref,
    vendored_consumer_paths,
)

ci_yaml = load_script("ci_yaml.py")

CI_FILE = REPO / ".gitlab-ci.yml"
PYTHON_TESTS_TEMPLATE = "python-tests.yml"

needs_pipeline = pytest.mark.skipif(not CI_FILE.is_file(), reason="no .gitlab-ci.yml")


def python_tests_changes(doc: dict) -> list[str]:
    """The `changes:` globs of the python-tests include, as written."""
    includes = doc.get("include") or []
    for entry in includes if isinstance(includes, list) else [includes]:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("file", "")).endswith(PYTHON_TESTS_TEMPLATE):
            return list((entry.get("inputs") or {}).get("changes") or [])
    return []


def _glob_re(pattern: str) -> re.Pattern[str]:
    """GitLab's `changes:` glob as a regex: `**` crosses directories, `*` does not."""
    out = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            # `**/` matches zero or more directories, so the pattern also selects
            # a file sitting directly in the prefix directory.
            out += "(?:[^/]+/)*"
            index += 3
        elif pattern.startswith("**", index):
            out += ".*"
            index += 2
        elif char == "*":
            out += "[^/]*"
            index += 1
        elif char == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(char)
            index += 1
    return re.compile(rf"^{out}$")


def unmatched(paths: list[str], globs: list[str]) -> list[str]:
    """Paths no glob in `globs` selects."""
    patterns = [_glob_re(glob) for glob in globs]
    return sorted({path for path in paths if not any(p.match(path) for p in patterns)})


@needs_pipeline
def test_every_vendored_copy_outside_scripts_runs_the_suite():
    changes = python_tests_changes(ci_yaml.load_ci(CI_FILE))
    assert changes, f"the {PYTHON_TESTS_TEMPLATE} include declares no changes: list"
    outside = [path for path in vendored_consumer_paths() if not path.startswith("scripts/")]
    assert outside, "the manifest registers nothing outside scripts/ — nothing to check"
    missing = unmatched(outside, changes)
    assert not missing, (
        f"the python-tests changes: list does not match {missing} — an edit to one "
        "skips tests/test_vendored_byte_identity.py, so the drift lands on main "
        "before any gate sees it. Add the path to that list."
    )


def test_the_glob_matcher_distinguishes_the_two_stars():
    """Mutation case: a dropped entry is reported, and `*` must not cross a
    directory boundary the way `**` does."""
    globs = ["ruff.toml", "scripts/**/*", "lint/*.yml"]
    assert unmatched(["ruff.toml", "scripts/a/b.py", "scripts/c.py", "lint/x.yml"], globs) == []
    assert unmatched(["ruff.toml"], ["scripts/**/*"]) == ["ruff.toml"]
    assert unmatched(["lint/sub/x.yml"], globs) == ["lint/sub/x.yml"]


# ---------------------------------------------------------------------------
# Tool provisioning for the jobs that touch live infrastructure
# ---------------------------------------------------------------------------

# Tools a job has to install, because no image in this pipeline ships them. A
# name outside this set (shell keywords, coreutils) is never asked about, so a
# newly-used tool enters the gate by being added here.
PROVISIONED_TOOLS = frozenset(
    {
        "amtool", "ansible", "ansible-galaxy", "ansible-lint", "ansible-playbook",
        "curl", "dig", "envsubst", "flux", "git", "gpg", "helm", "jq",
        "kubeconform", "kubectl", "kustomize", "nc", "nslookup", "op", "promtool",
        "shellcheck", "ssh", "task", "terraform", "yamllint", "yq",
    }
)

# Package -> what it puts on PATH, for `apt-get install` and `apk add`.
PACKAGE_TOOLS = {
    "1password-cli": {"op"},
    "bind-tools": {"dig", "nslookup"},
    "curl": {"curl"},
    "dnsutils": {"dig", "nslookup"},
    "gettext": {"envsubst"},
    "gettext-base": {"envsubst"},
    "git": {"git"},
    "gnupg": {"gpg"},
    "jq": {"jq"},
    "netcat-openbsd": {"nc"},
    "openssh": {"ssh"},
    "openssh-client": {"ssh"},
}

# pip requirement -> the CLIs it installs.
PIP_TOOLS = {
    "ansible": {"ansible", "ansible-galaxy", "ansible-playbook"},
    "ansible-core": {"ansible", "ansible-galaxy", "ansible-playbook"},
    "ansible-lint": {"ansible-lint"},
    "yamllint": {"yamllint"},
}

# Third-party module a gate imports -> the pip requirements that satisfy it.
# ansible pulls PyYAML in, which is why the maintenance jobs need no pyyaml pin.
PIP_MODULES = {"yaml": {"pyyaml", "ansible", "ansible-core"}, "requests": {"requests"}}

# What each job image ships, keyed by the full `repository:tag` or by the
# repository alone where the tag does not change the tool set. An image absent
# here provides nothing, so a job on a new image installs what its scripts call.
IMAGE_TOOLS = {
    "python:3.11-slim": frozenset(),
    "python:3.13-slim": frozenset(),
    "python:3.13": frozenset({"curl", "git", "ssh"}),
    "alpine:3.23": frozenset(),
    "hashicorp/terraform": frozenset({"terraform", "git", "ssh"}),
}


def image_tools(image: str) -> frozenset:
    """What an image ships: its exact pin, else its repository, else nothing."""
    if image in IMAGE_TOOLS:
        return IMAGE_TOOLS[image]
    return IMAGE_TOOLS.get(image.split(":")[0], frozenset())

# A tool a sourced helper offers on a branch the CI caller never takes, keyed
# "<script>:<tool>" with why the job need not install it.
OFF_CI_PATH: dict[str, str] = {}

# Fragments whose descendants deploy, verify or maintain the live estate.
LIVE_BASES = (".deploy-base", ".terraform-drift-plan")
LIVE_STAGES = ("deploy", "verify", "maintenance")
# Anchors, so a rename cannot leave the gate inspecting an empty job set.
LIVE_ANCHORS = ("deploy-ansible-base", "deploy-verify", "cluster-verify")
# The one live job that `!reference`s .kubectl-setup, so the fragment check
# reads a job that really consumes it.
KUBECTL_JOB = "cluster-verify"

_TOOL_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
# Command-position separators. Quoting is not tracked, so a `bash -c '...; ...'`
# payload splits like any other list and its commands are seen.
_SEGMENT = re.compile(r"\|\||&&|\$\(|<<<|[|;()`&{}\n]")
_SHELL_WORDS = frozenset(
    {
        "case", "do", "done", "elif", "else", "esac", "env", "eval", "exec", "fi",
        "for", "if", "in", "nohup", "sudo", "then", "time", "until", "while", "!",
    }
)
_SCRIPT_REF = re.compile(
    r"(?:scripts/|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?/)([A-Za-z0-9_.-]+\.(?:sh|py))"
)
_VAR_REF = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)
_INPUT_REF = re.compile(r"\$\[\[\s*inputs\.([A-Za-z0-9_]+)\s*\]\]")

# Top-level keys GitLab reserves; every other key in a jobs document is a job.
CI_RESERVED = frozenset(
    {
        "default", "include", "image", "services", "stages", "variables",
        "workflow", "before_script", "after_script", "cache", "spec",
    }
)


def strip_shell_comments(text: str) -> str:
    """Shell text with `#` comments dropped, so prose naming a tool is not read
    as a call. A `#` inside a quoted string goes with them."""
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


def shell_commands(text: str) -> set:
    """Tools from PROVISIONED_TOOLS the text calls: the first word of each
    command segment, plus the word after `--` and after `command -v`. A command
    handed to another runtime (`kubectl run -- sh -c ...`) is not counted."""
    found = set()
    for segment in _SEGMENT.split(strip_shell_comments(text)):
        words = [word.strip("\"'") for word in segment.split()]
        head = 0
        while head < len(words) and (
            words[head] in _SHELL_WORDS
            or ("=" in words[head] and not words[head].startswith("-"))
        ):
            head += 1
        if head < len(words) and _TOOL_TOKEN.fullmatch(words[head]):
            found.add(words[head])
        for index, word in enumerate(words[:-1]):
            nxt = words[index + 1]
            if word == "--" and _TOOL_TOKEN.fullmatch(nxt):
                found.add(nxt)
            if word == "command" and nxt == "-v" and index + 2 < len(words):
                if _TOOL_TOKEN.fullmatch(words[index + 2]):
                    found.add(words[index + 2])
    return found & PROVISIONED_TOOLS


def shell_installs(text: str) -> set:
    """What the text puts on PATH: package installs, pip requirements, a
    verified binary dropped in /usr/local/bin, a ci-fetch-tools.py fetch.
    Modules appear as `py:<module>`; install ordering is not modelled."""
    got = set()
    body = strip_shell_comments(text)
    pattern = (
        r"(?:apt-get|apk)\s+(?:install|add)"
        r"((?:\s+-{1,2}[A-Za-z-]+)*(?:\s+[^\n;&|]*))"
    )
    for match in re.finditer(pattern, body):
        for word in match.group(1).split():
            if not word.startswith("-"):
                got |= PACKAGE_TOOLS.get(word.strip("\"'"), set())
    for match in re.finditer(r"pip\s+install([^\n;&|]*)", body):
        for word in match.group(1).split():
            if word.startswith("-") or word.startswith("$"):
                continue
            name = re.split(r"[=<>!~\[]", word.strip("\"'"))[0].strip().lower()
            got |= PIP_TOOLS.get(name, set())
            got |= {f"py:{mod}" for mod, reqs in PIP_MODULES.items() if name in reqs}
    for match in re.finditer(r"install\s+-m\s+\S+\s+\S+\s+/usr/local/bin/(\S+)", body):
        got.add(match.group(1))
    for match in re.finditer(r"tar\s+[a-z]+\s+\S+\s+-C\s+/usr/local/bin\s+(\S+)", body):
        got.add(match.group(1))
    for match in re.finditer(r"ci-fetch-tools\.py([^\n;&|'\"]*)", body):
        got |= {word for word in match.group(1).split() if not word.startswith("-")}
    if "1password-cli" in body:
        got.add("op")
    return got


def unprovisioned(image: str, text: str, also_required=(), excused=()) -> list:
    """Tools and modules the job needs and nothing in it provides."""
    provided = set(image_tools(image)) | shell_installs(text)
    required = (shell_commands(text) | set(also_required)) - set(excused)
    return sorted(required - provided)


def python_modules(path, seen=None) -> set:
    """Third-party modules a gate needs, as `py:<module>`, following the sibling
    modules it imports — gate_common.py is where a `kubectl -o json` gate picks
    PyYAML up."""
    path = Path(path)
    seen = set() if seen is None else seen
    if not path.is_file() or path in seen:
        return set()
    seen.add(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module.split(".")[0])
    need = set()
    for name in names:
        sibling = path.parent / f"{name}.py"
        if sibling.is_file():
            need |= python_modules(sibling, seen)
        elif name in PIP_MODULES:
            need.add(f"py:{name}")
    return need


def script_closure(names, seen=None, found=None) -> dict:
    """Every repo script reached from `names`: the shell text to scan, the
    modules its python gates import, and the off-path tools to excuse."""
    seen = set() if seen is None else seen
    found = {"text": [], "modules": set(), "excused": set()} if found is None else found
    for name in sorted(names):
        path = SCRIPTS / name
        if name in seen or not path.is_file():
            continue
        seen.add(name)
        if name.endswith(".py"):
            found["modules"] |= python_modules(path)
            continue
        text = path.read_text(encoding="utf-8")
        found["text"].append(text)
        used = shell_commands(text)
        found["excused"] |= {tool for tool in used if f"{name}:{tool}" in OFF_CI_PATH}
        script_closure(set(_SCRIPT_REF.findall(strip_shell_comments(text))), seen, found)
    return found


def _substitute_inputs(node, inputs: dict):
    """A library template with its `$[[ inputs.x ]]` resolved."""
    if isinstance(node, str):
        return _INPUT_REF.sub(
            lambda m: "" if inputs.get(m.group(1)) is None else str(inputs[m.group(1)]),
            node,
        )
    if isinstance(node, ci_yaml.Reference):
        return ci_yaml.Reference([_substitute_inputs(item, inputs) for item in node])
    if isinstance(node, list):
        return [_substitute_inputs(item, inputs) for item in node]
    if isinstance(node, dict):
        return {
            _substitute_inputs(key, inputs): _substitute_inputs(value, inputs)
            for key, value in node.items()
        }
    return node


def library_fragments(doc: dict) -> dict:
    """The hidden jobs the library include: block contributes, so a `!reference`
    into one resolves to the fragment this pipeline actually gets."""
    ref = pinned_lib_ref()
    frags: dict = {}
    for entry in doc.get("include") or []:
        if not isinstance(entry, dict) or "file" not in entry:
            continue
        files = entry["file"] if isinstance(entry["file"], list) else [entry["file"]]
        for spec_file in files:
            text = lib_file(str(spec_file).lstrip("/"), ref)
            docs = [
                d
                for d in yaml.load_all(text, Loader=ci_yaml.CILoader)
                if isinstance(d, dict)
            ]
            if not docs:
                continue
            inputs = {}
            if "spec" in docs[0]:
                decls = (docs[0].get("spec") or {}).get("inputs") or {}
                for name, decl in decls.items():
                    inputs[name] = decl.get("default") if isinstance(decl, dict) else None
            inputs.update(entry.get("inputs") or {})
            for key, body in docs[-1].items():
                if not isinstance(key, str) or not isinstance(body, dict):
                    continue
                name = _substitute_inputs(key, inputs)
                if name:
                    frags.setdefault(name, _substitute_inputs(body, inputs))
    return frags


def pipeline_doc() -> dict:
    """.gitlab-ci.yml with `!reference` kept and the library fragments merged in."""
    doc = ci_yaml.load_ci(CI_FILE)
    for name, body in library_fragments(doc).items():
        doc.setdefault(name, body)
    return doc


def extends_chain(doc: dict, name: str, seen=()) -> list:
    """A job's `extends` ancestry, least derived first."""
    body = doc.get(name)
    if not isinstance(body, dict) or name in seen:
        return []
    parents = body.get("extends")
    parents = [parents] if isinstance(parents, str) else (parents or [])
    chain = []
    for parent in parents:
        chain += extends_chain(doc, parent, seen + (name,))
    return chain + [name]


def job_shell(doc: dict, name: str) -> tuple:
    """A job's image and the shell it runs. A job-level `before_script` replaces
    the inherited one, so the most derived definition of each block wins."""
    chain = extends_chain(doc, name)
    variables = {k: str(v) for k, v in (doc.get("variables") or {}).items()}
    image = (doc.get("default") or {}).get("image")
    for link in chain:
        variables.update(
            {k: str(v) for k, v in (doc[link].get("variables") or {}).items()}
        )
        if doc[link].get("image"):
            image = doc[link]["image"]
    if isinstance(image, dict):
        image = image.get("name")
    lines = []
    for block in ("before_script", "script", "after_script"):
        for link in reversed(chain):
            if block in doc[link]:
                lines += ci_yaml.script_lines(doc[link], doc, block)
                break

    def expand(raw: str) -> str:
        return _VAR_REF.sub(
            lambda m: variables.get(m.group(1) or m.group(2), m.group(0)), raw
        )

    text = "\n".join(lines)
    for _ in range(3):
        text = expand(text)
    return expand(str(image or "")).split("@")[0], text


def live_jobs(doc: dict) -> list:
    """Jobs that deploy, verify or maintain the live estate."""
    return sorted(
        name
        for name, body in doc.items()
        if isinstance(body, dict)
        and not name.startswith(".")
        and name not in CI_RESERVED
        and (
            body.get("stage") in LIVE_STAGES
            or any(base in LIVE_BASES for base in extends_chain(doc, name))
        )
    )


def job_missing_tools(doc: dict, name: str) -> list:
    image, text = job_shell(doc, name)
    reached = script_closure(set(_SCRIPT_REF.findall(strip_shell_comments(text))))
    return unprovisioned(
        image,
        "\n".join([text] + reached["text"]),
        also_required=reached["modules"],
        excused=reached["excused"],
    )


@needs_pipeline
class TestLiveJobToolProvisioning:
    """A script that calls a binary no job image ships, and that the job's
    before_script never installs, fails at runtime rather than at lint."""

    def test_every_live_job_provides_what_its_scripts_call(self):
        doc = pipeline_doc()
        missing = {name: job_missing_tools(doc, name) for name in live_jobs(doc)}
        missing = {name: tools for name, tools in missing.items() if tools}
        assert not missing, (
            "these jobs run a script that calls a tool or imports a module "
            "nothing in the job installs, so they fail at runtime rather than "
            f"at lint: {missing}. Install it in the job's before_script "
            "(scripts/ci-fetch-tools.py for a pinned static binary), or record "
            "it in IMAGE_TOOLS if the job image ships it."
        )

    def test_the_subject_set_covers_the_live_jobs(self):
        jobs = live_jobs(pipeline_doc())
        assert len(jobs) > 5, f"only {len(jobs)} live jobs found — the gate read almost nothing"
        for anchor in LIVE_ANCHORS:
            assert anchor in jobs, f"{anchor} is no longer a live job; fix LIVE_BASES/LIVE_STAGES"

    def test_the_library_fragments_resolve(self):
        """`!reference [.kubectl-setup, before_script]` must resolve, or every
        tool the fragment installs reads as absent and the gate is noise."""
        doc = pipeline_doc()
        for fragment in (".deploy-base", ".kubectl-setup", ".install-1password"):
            assert isinstance(doc.get(fragment), dict), (
                f"{fragment} did not resolve from the library include block"
            )
        _, text = job_shell(doc, KUBECTL_JOB)
        assert "kubectl" in shell_installs(text), (
            ".kubectl-setup no longer installs a kubectl the gate can see"
        )

    def test_an_argument_is_not_read_as_a_command(self):
        """The precision the gate depends on: a tool name in an argument
        position is not a call."""
        assert shell_commands('git diff --quiet -- "ansible/x.yml"') == {"git"}
        assert shell_commands("flux reconcile source git flux-system") == {"flux"}
        assert shell_commands("# run task hosts:sync first") == set()
        assert shell_commands("op run -- ansible-playbook -i inventories/prod site.yml") == {
            "op",
            "ansible-playbook",
        }

    def test_a_dropped_install_is_detected(self):
        """The mutation: the fragment that installed jq stops installing it."""
        with_jq = "apt-get install -y -qq jq\nkubectl get pods -o json | jq .items"
        assert unprovisioned("python:3.13-slim", with_jq) == ["kubectl"]
        without_jq = "kubectl get pods -o json | jq .items"
        assert unprovisioned("python:3.13-slim", without_jq) == ["jq", "kubectl"]

    def test_a_dropped_fetch_is_detected(self):
        """The repo's own provisioning path: scripts/ci-fetch-tools.py."""
        fetched = "python3 scripts/ci-fetch-tools.py jq amtool\namtool check-config x\njq ."
        assert unprovisioned("python:3.13-slim", fetched) == []
        assert unprovisioned("python:3.13-slim", "amtool check-config x\njq .") == [
            "amtool",
            "jq",
        ]

    def test_a_missing_python_module_is_detected(self):
        """The class a live gate hits: it imports PyYAML through gate_common.py
        and the slim image ships none."""
        assert unprovisioned("python:3.13-slim", "true", also_required={"py:yaml"}) == [
            "py:yaml"
        ]
        pinned = 'pip install --quiet "pyyaml==6.0.2"'
        assert unprovisioned("python:3.13-slim", pinned, also_required={"py:yaml"}) == []
        assert (
            unprovisioned(
                "python:3.13-slim",
                'pip install --quiet "ansible-core==2.21.5"',
                also_required={"py:yaml"},
            )
            == []
        )

    def test_an_off_path_tool_is_excused_only_when_declared(self):
        probe = "nc -z -w 5 host 22"
        assert unprovisioned("python:3.13-slim", probe) == ["nc"]
        assert unprovisioned("python:3.13-slim", probe, excused={"nc"}) == []
