"""An `-e` extra var arrives as a string, which ansible-core refuses in a `when:`.

A conditional naming one ENDS its chain in `| bool` or compares it. The fail-closed
walk globs a templated include (`_glob_candidates`) and follows `action:` forms.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import pytest
import yaml
from conftest import REPO, load_script

ANSIBLE = "ansible"

# Every file that hands an `ansible-playbook` call an `-e name=value` pair.
CALLER_GLOBS = (
    ".gitlab-ci.yml",
    ".gitlab/ci/*.yml",
    "Taskfile.yml",
    "taskfiles/*.yml",
    "scripts/*.sh",
)

EXTRA_VAR_FLAGS = frozenset({"-e", "--extra-vars"})
# One `name=value` pair, the only `-e` form carrying a readable variable name.
ASSIGNMENT = re.compile(r"^([A-Za-z_]\w*)=")

INCLUDE_KEYS = frozenset(
    {
        "ansible.builtin.include_tasks",
        "ansible.builtin.import_tasks",
        "ansible.builtin.include_playbook",
        "ansible.builtin.import_playbook",
        "include_tasks",
        "import_tasks",
        "import_playbook",
    }
)

ROLE_KEYS = frozenset(
    {
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
        "include_role",
        "import_role",
    }
)

# `action:` and `local_action:` name their module in the value, so an include or
# role invoked that way is an edge the key-form walk above never sees.
ACTION_KEYS = frozenset({"action", "local_action"})
# The parameter each module form carries its target in.
INCLUDE_TARGET_PARAMS = ("file", "_raw_params")
ROLE_TARGET_PARAMS = ("name", "_raw_params")

COLLECTION = "weisssrv.infra"
COLLECTION_ROLES = Path("ansible_collections/weisssrv/infra/roles")
# ansible honours either spelling, and the CI jobs point a scratch install at it.
COLLECTIONS_PATH_ENV = ("ANSIBLE_COLLECTIONS_PATH", "ANSIBLE_COLLECTIONS_PATHS")
# Where `task lib:sync` puts the checkout the python-tests job clones.
LOCAL_CHECKOUT = ".weisssrv-lib"
ROLE_SUBDIRS = ("tasks", "handlers")
ROLE_META = Path("meta/main.yml")

# A reference that yields a real boolean on its own, so `| bool` is redundant.
COMPARISON = re.compile(r"^\s*(==|!=|<=|>=|<|>|\bin\b|\bis\b|\bnot\s+in\b)")

# A `{{ ... }}` in an include target becomes `*`, so the walk inspects every
# sibling the expression could name rather than guessing the runtime value.
TEMPLATE_EXPRESSION = re.compile(r"\{\{.*?\}\}")
TASK_FILE_SUFFIXES = (".yml", ".yaml")


class Uninspectable(RuntimeError):
    """A reachable file or role the walk cannot read, so a pass would be a lie."""


def _invocations(text: str) -> list[dict]:
    """The `ansible-playbook` calls in one caller file, via the shared parser."""
    return load_script("ci_playbook_invocations.py").parse_invocations(text)


def _argv_tokens(argv: str) -> list[str]:
    """One argv as tokens; an unbalanced quote falls back to a plain split."""
    try:
        return shlex.split(argv)
    except ValueError:
        return argv.split()


def _extra_vars_value(tokens: list[str], index: int) -> tuple[str | None, int]:
    """The first value token of an extra-vars flag, and where to resume."""
    token = tokens[index]
    flag, sep, attached = token.partition("=")
    if sep and flag in EXTRA_VAR_FLAGS:
        return attached, index + 1
    if token in EXTRA_VAR_FLAGS:
        if index + 1 >= len(tokens):
            # A trailing bare flag names nothing, so report it rather than skip it.
            return token, index + 1
        return tokens[index + 1], index + 2
    if token.startswith("-e") and len(token) > 2:
        return token[2:], index + 1
    return None, index + 1


def extra_var_names(argv: str) -> tuple[set[str], list[str]]:
    """Every `-e` name in one argv, plus the values carrying no name at all."""
    names: set[str] = set()
    unreadable: list[str] = []
    tokens = _argv_tokens(argv)
    index = 0
    while index < len(tokens):
        value, index = _extra_vars_value(tokens, index)
        if value is None:
            continue
        # A quoted multi-assignment value arrives as one token per pair once the
        # shared parser has rejoined it, so each is read in turn.
        for pair in _argv_tokens(value) or [value]:
            first = ASSIGNMENT.match(pair)
            if first is None:
                unreadable.append(pair)
                continue
            names.add(first.group(1))
        while index < len(tokens) and (more := ASSIGNMENT.match(tokens[index])):
            names.add(more.group(1))
            index += 1
    return names, unreadable


def _scan_callers(repo: Path) -> tuple[dict[str, set[str]], list[tuple[str, str]]]:
    """playbook -> its callers' `-e` names, plus every value with no name in it."""
    found: dict[str, set[str]] = {}
    unreadable: list[tuple[str, str]] = []
    for glob in CALLER_GLOBS:
        for caller in sorted(repo.glob(glob)):
            if not caller.is_file():
                continue
            rel = caller.relative_to(repo).as_posix()
            for call in _invocations(caller.read_text(encoding="utf-8")):
                names, opaque = extra_var_names(call["argv"])
                if names:
                    found.setdefault(call["playbook"], set()).update(names)
                unreadable += [(rel, value) for value in opaque]
    return found, unreadable


def extra_vars_by_playbook(repo: Path = REPO) -> dict[str, set[str]]:
    """playbook path (as written) -> the `-e` names its callers pass it."""
    return _scan_callers(repo)[0]


def unreadable_extra_vars(repo: Path = REPO) -> list[tuple[str, str]]:
    """(caller, value) for every `-e @file` or JSON blob hiding its names."""
    return _scan_callers(repo)[1]


def _label(path: Path, repo: Path) -> str:
    """A path for a failure message: repo-relative, or the role's own tail."""
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return "/".join(path.parts[-4:])


def _resolve(playbook: str, repo: Path, base: Path | None = None) -> Path | None:
    """A playbook or include path as written, against its own dir then ansible/."""
    candidates = []
    if base is not None:
        candidates.append(base / playbook)
    candidates += [repo / ANSIBLE / playbook, repo / playbook]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _collection_roots(repo: Path) -> list[Path]:
    """Every roles directory the collection may be read from, ansible's order first."""
    bases = [
        Path(entry).expanduser()
        for var in COLLECTIONS_PATH_ENV
        for entry in os.environ.get(var, "").split(os.pathsep)
        if entry
    ]
    bases += [
        repo / ".ansible-home/collections",
        Path.home() / ".ansible/collections",
        Path(os.environ.get("WEISSSRV_LIB_PATH") or repo / LOCAL_CHECKOUT),
        repo.parent / "weisssrv-lib",
    ]
    return [base / COLLECTION_ROLES for base in bases]


def resolve_role(name: str, repo: Path = REPO) -> Path:
    """A role name to its directory; unreadable is a failure, never a skip."""
    candidates = [repo / ANSIBLE / "roles" / name]
    if name.startswith(f"{COLLECTION}."):
        short = name.split(".")[-1]
        candidates += [root / short for root in _collection_roots(repo)]
    for candidate in candidates:
        if any((candidate / part).is_dir() for part in ROLE_SUBDIRS):
            return candidate
        if (candidate / ROLE_META).is_file():
            return candidate
    raise Uninspectable(
        f"role {name} is not readable, so its conditionals go unchecked. Install "
        "the pinned collection: `ansible-galaxy collection install -r "
        "ansible/requirements.yml -p .ansible-home/collections`"
    )


def _role_files(role: Path) -> list[Path]:
    """Every task, handler and meta file in a role — all of it runs."""
    files = sorted(
        path
        for part in ROLE_SUBDIRS
        for pattern in ("*.yml", "*.yaml")
        for path in (role / part).rglob(pattern)
        if path.is_file()
    )
    meta = role / ROLE_META
    return files + ([meta] if meta.is_file() else [])


def _load(path: Path):
    """One YAML file, or a failure: a skipped file is coverage lost in silence."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        raise Uninspectable(
            f"{path} does not parse, so its conditionals go unchecked: {exc}"
        ) from exc


def _walk(node, key_wanted: str):
    """Every value stored under `key_wanted` anywhere in a parsed YAML tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == key_wanted:
                yield value
            yield from _walk(value, key_wanted)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, key_wanted)


def _include_targets(doc, origin: str) -> list[str]:
    """Every task file or playbook an include or import names, as written."""
    targets = []
    for key in INCLUDE_KEYS:
        for value in _walk(doc, key):
            target = value.get("file") if isinstance(value, dict) else value
            if not isinstance(target, str) or not target.strip():
                raise Uninspectable(
                    f"{origin}: {key} names no file this gate can read ({target!r}), "
                    "so what it runs goes unchecked"
                )
            targets.append(target.strip())
    return targets


def _glob_candidates(target: str, repo: Path, base: Path) -> list[Path]:
    """Every `.yml` a templated include target could name, by static-prefix glob.

    Each `{{ ... }}` becomes `*`, so `disks-{{ backend }}.yml` yields every
    `disks-*.yml`. Over-approximating is safe; matching nothing is Uninspectable.
    """
    pattern = TEMPLATE_EXPRESSION.sub("*", target)
    for root in (base, repo / ANSIBLE, repo):
        try:
            matches = sorted(root.glob(pattern))
        except (ValueError, NotImplementedError, OSError):
            continue
        found = [
            path.resolve()
            for path in matches
            if path.is_file() and path.suffix in TASK_FILE_SUFFIXES
        ]
        if found:
            return found
    return []


def _resolve_target(target: str, repo: Path, origin: Path) -> list[Path]:
    """One include target to the file(s) it can run; unresolvable fails the gate."""
    label = _label(origin, repo)
    if "{{" in target:
        candidates = _glob_candidates(target, repo, origin.parent)
        if candidates:
            return candidates
        raise Uninspectable(
            f"{label}: templated include target `{target}` matches no file, so what "
            "it runs goes unchecked. Give the candidates one static prefix that "
            f"`{TEMPLATE_EXPRESSION.sub('*', target)}` finds, or inline the include"
        )
    resolved = _resolve(target, repo, base=origin.parent)
    if resolved is None:
        raise Uninspectable(
            f"{label}: include target `{target}` resolves to no file, so what it "
            "runs goes unchecked"
        )
    return [resolved]


def _entry_role(entry) -> str | None:
    """The role name in one `roles:` or `dependencies:` entry, string or dict."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        name = entry.get("role") or entry.get("name")
        return name if isinstance(name, str) else None
    return None


def _role_entries(doc, meta: bool) -> list[tuple[str, object]]:
    """(key, entry) for every role a file names, before any of them is read."""
    entries = []
    for key in ["roles", "dependencies"] if meta else ["roles"]:
        for value in _walk(doc, key):
            # ansible requires a list here, so any other value is unrelated data.
            if isinstance(value, list):
                entries += [(key, entry) for entry in value]
    for key in ROLE_KEYS:
        entries += [(key, value) for value in _walk(doc, key)]
    return entries


def _role_names(doc, origin: str, meta: bool = False) -> list[str]:
    """Every role a file names; one this gate cannot name fails it."""
    names = []
    for key, entry in _role_entries(doc, meta):
        name = _entry_role(entry)
        if name is None:
            raise Uninspectable(
                f"{origin}: {key} entry {entry!r} names no role, so the role it "
                "runs goes unchecked"
            )
        if "{{" in name:
            raise Uninspectable(
                f"{origin}: {key} names the templated role `{name}`, so the role it "
                "runs goes unchecked. Name the role literally"
            )
        names.append(name)
    return names


def _action_params(rest: str) -> dict:
    """One free-form action's parameters: `k=v` pairs plus any positional.

    A positional target stays under `_raw_params` even beside `k=v` pairs, so
    `include_tasks steps.yml apply=...` keeps naming the file it runs.
    """
    params: dict = {}
    positional: list[str] = []
    for token in rest.split():
        name, separator, assigned = token.partition("=")
        if separator:
            params[name] = assigned
        else:
            positional.append(token)
    if positional:
        params["_raw_params"] = " ".join(positional)
    return params


def _action_module(value) -> tuple[str, dict] | None:
    """(module, parameters) for one `action:` value, or None if it names none.

    Both spellings: the free-form string `include_tasks foo.yml` (or
    `file=foo.yml`) and the dict form carrying `module:`.
    """
    if isinstance(value, str):
        module, _, rest = value.strip().partition(" ")
        return module, _action_params(rest.strip())
    if isinstance(value, dict) and isinstance(value.get("module"), str):
        return value["module"].strip(), {
            key: item for key, item in value.items() if key != "module"
        }
    return None


def _action_target(module: str, params: dict, origin: str, key: str, names) -> str:
    """The target one action form carries; anything else fails the gate."""
    for param in names:
        target = params.get(param)
        if isinstance(target, str) and target.strip():
            return target.strip()
    raise Uninspectable(
        f"{origin}: {key} invokes {module} with no plain-string target "
        f"({params!r}), so what it runs goes unchecked. Use the `{module}:` key "
        "form, or name the target literally"
    )


def _action_edges(doc, origin: str) -> tuple[list[str], list[str]]:
    """(include targets, role names) reached through `action:`/`local_action:`."""
    targets: list[str] = []
    roles: list[str] = []
    for key in sorted(ACTION_KEYS):
        for value in _walk(doc, key):
            found = _action_module(value)
            if found is None:
                continue
            module, params = found
            if module in INCLUDE_KEYS:
                targets.append(
                    _action_target(module, params, origin, key, INCLUDE_TARGET_PARAMS)
                )
            elif module in ROLE_KEYS:
                name = _action_target(module, params, origin, key, ROLE_TARGET_PARAMS)
                if "{{" in name:
                    raise Uninspectable(
                        f"{origin}: {key} names the templated role `{name}`, so "
                        "the role it runs goes unchecked. Name the role literally"
                    )
                roles.append(name)
    return targets, roles


def reachable_files(playbook: str, repo: Path = REPO) -> list[Path]:
    """The playbook plus every task file, playbook and role it transitively runs."""
    start = _resolve(playbook, repo)
    if start is None:
        raise Uninspectable(
            f"playbook {playbook} resolves to no file, so every conditional its "
            "`-e` caller reaches goes unchecked"
        )
    seen: dict[Path, None] = {start: None}
    queue = [start]
    roles_seen: set[str] = set()
    while queue:
        current = queue.pop()
        doc = _load(current)
        label = _label(current, repo)
        action_targets, action_roles = _action_edges(doc, label)
        targets = [
            resolved
            for target in _include_targets(doc, label) + action_targets
            for resolved in _resolve_target(target, repo, current)
        ]
        named = _role_names(doc, label, meta=current.match(str(ROLE_META)))
        for name in named + action_roles:
            if name in roles_seen:
                continue
            roles_seen.add(name)
            targets += _role_files(resolve_role(name, repo))
        for target in targets:
            if target not in seen:
                seen[target] = None
                queue.append(target)
    return list(seen)


def _conditions(doc) -> list[str]:
    """Every `when:` expression in a file, one string per clause."""
    clauses = []
    for value in _walk(doc, "when"):
        for clause in value if isinstance(value, list) else [value]:
            if isinstance(clause, str):
                clauses.append(clause)
    return clauses


def _is_booled(expression: str, name: str) -> bool:
    """Does every reference to `name` in one clause yield a real boolean?

    `bool` must END the chain: `x | bool | string` is a string again, and a
    later filter can undo the coercion just as readily.
    """
    pattern = re.compile(
        r"\b" + re.escape(name) + r"\b((?:\s*\|\s*\w+(?:\([^()]*\))?)*)"
    )
    for match in pattern.finditer(expression):
        chain = [f.strip() for f in match.group(1).split("|") if f.strip()]
        last = chain[-1].split("(")[0] if chain else ""
        if last == "bool":
            continue
        # A comparison yields a boolean whatever the chain ended in.
        if COMPARISON.match(expression[match.end():]):
            continue
        return False
    return True


def unbooled_conditionals(repo: Path = REPO) -> list[tuple[str, str, str]]:
    """(file, extra-var, clause) for every conditional missing its `| bool`."""
    offenders = []
    for playbook, names in sorted(extra_vars_by_playbook(repo).items()):
        for path in reachable_files(playbook, repo):
            for clause in _conditions(_load(path)):
                for name in sorted(names):
                    if re.search(r"\b" + re.escape(name) + r"\b", clause):
                        if not _is_booled(clause, name):
                            offenders.append((_label(path, repo), name, clause))
    return offenders


class TestDiscovery:
    """Guards against a vacuous pass: the walk must find real work to check."""

    def test_the_pipeline_extra_vars_are_found(self):
        found = extra_vars_by_playbook()
        names = {n for names in found.values() for n in names}
        assert names, (
            "no caller passes `-e name=value`, so this gate checked nothing. A "
            "removed extra var retires it; a parse regression does not"
        )

    def test_an_included_task_file_is_reached(self):
        reached = {
            p.relative_to(REPO).as_posix()
            for p in reachable_files("playbooks/maintenance/update-packages.yml")
        }
        assert "ansible/playbooks/maintenance/_reboot-if-needed.yml" in reached, reached

    def test_every_caller_playbook_walks_clean(self):
        """The fail-closed walk must still cover the real tree end to end: an
        edge it cannot follow now raises instead of narrowing the set."""
        for playbook in sorted(extra_vars_by_playbook()):
            assert reachable_files(playbook), playbook

    def test_every_collection_role_a_caller_reaches_is_walked(self):
        """`roles:` and `include_role:` are reachability edges too: an uncoerced
        role conditional fails the same play. A cluster naming none walks none."""
        for playbook in sorted(extra_vars_by_playbook()):
            reached = reachable_files(playbook)
            named = {
                name
                for path in reached
                for name in _role_names(
                    _load(path), _label(path, REPO), meta=path.match(str(ROLE_META))
                )
                if name.startswith(f"{COLLECTION}.")
            }
            for name in sorted(named):
                files = set(_role_files(resolve_role(name)))
                assert files and files <= set(reached), name


def test_every_extra_var_value_names_its_variables():
    unreadable = unreadable_extra_vars()
    assert not unreadable, (
        "an `-e` value carries no `name=value` pair, so the names it sets go "
        "unchecked — teach this gate to read the form or pass them inline: "
        + "; ".join(f"{caller}: `{value}`" for caller, value in unreadable)
    )


def test_every_extra_var_conditional_is_booled():
    offenders = unbooled_conditionals()
    assert not offenders, (
        "a `when:` reads a `-e` extra var without `| bool`, so ansible-core "
        "refuses the str-derived result: "
        + "; ".join(f"{f}: {name} in `{clause}`" for f, name, clause in offenders)
    )


BUGGY_PLAYBOOK = """---
- name: Probe
  hosts: all
  tasks:
    - name: Exercise something
      ansible.builtin.command: /bin/true
      when:
        - probe_exercise | default(true)
"""

ROLE_PLAYBOOK = """---
- name: Probe
  hosts: all
  roles:
    - role: probe_role
"""

CI_CALL = (
    "verify:\n"
    "  script:\n"
    "    - ansible-playbook -i inventories/prod playbooks/probe.yml {args}\n"
)


def _fixture_repo(
    tmp_path: Path, playbook_body: str, args: str = "-e probe_exercise=false"
) -> Path:
    (tmp_path / "ansible/playbooks").mkdir(parents=True)
    (tmp_path / "ansible/playbooks/probe.yml").write_text(playbook_body)
    (tmp_path / ".gitlab-ci.yml").write_text(CI_CALL.format(args=args))
    return tmp_path


def _fixture_role(repo: Path, body: str, part: str = "tasks") -> Path:
    """A `probe_role` whose one task file carries the conditional under test."""
    role = repo / "ansible/roles/probe_role" / part
    role.mkdir(parents=True)
    (role / "main.yml").write_text(body)
    return role / "main.yml"


TASK_FILE = """---
- name: Exercise something
  ansible.builtin.command: /bin/true
  when: probe_exercise | default(true){suffix}
"""


def test_a_string_typed_conditional_is_reported(tmp_path):
    """Mutation case: the shape that fails a play given `-e name=false`."""
    offenders = unbooled_conditionals(_fixture_repo(tmp_path, BUGGY_PLAYBOOK))
    assert offenders == [
        (
            "ansible/playbooks/probe.yml",
            "probe_exercise",
            "probe_exercise | default(true)",
        )
    ]


@pytest.mark.parametrize(
    "clause",
    [
        "probe_exercise | default(true) | bool",
        "not (probe_exercise | default(true) | bool)",
        "probe_exercise | default('') == 'yes'",
        # A comparison yields a boolean whatever the chain ended in.
        "probe_exercise | default(true) | bool | string == 'True'",
    ],
)
def test_a_boolean_valued_conditional_passes(tmp_path, clause):
    body = BUGGY_PLAYBOOK.replace("probe_exercise | default(true)", clause)
    assert unbooled_conditionals(_fixture_repo(tmp_path, body)) == []


@pytest.mark.parametrize(
    "clause",
    [
        "probe_exercise | default(true) | bool | string",
        "probe_exercise | default(true) | bool | lower",
        "probe_exercise | bool | default(true)",
    ],
)
def test_a_bool_undone_by_a_later_filter_is_reported(tmp_path, clause):
    """`| bool` must END the chain: a filter after it re-derives a string."""
    body = BUGGY_PLAYBOOK.replace("probe_exercise | default(true)", clause)
    offenders = unbooled_conditionals(_fixture_repo(tmp_path, body))
    assert [name for _, name, _ in offenders] == ["probe_exercise"], offenders


class TestExtraVarParsing:
    """Every assignment in an `-e` argument, in each spelling a caller uses."""

    @pytest.mark.parametrize(
        "args",
        [
            '-e "probe_exercise=false probe_other=false"',
            "-e probe_exercise=false -e probe_other=false",
            "--extra-vars=probe_exercise=false --extra-vars probe_other=false",
            "-eprobe_exercise=false -e probe_other=false",
        ],
    )
    def test_every_assignment_in_the_argument_is_read(self, tmp_path, args):
        found = extra_vars_by_playbook(_fixture_repo(tmp_path, BUGGY_PLAYBOOK, args))
        assert found == {"playbooks/probe.yml": {"probe_exercise", "probe_other"}}

    def test_a_second_assignment_in_a_quoted_value_is_checked(self, tmp_path):
        """A quoted value carrying two assignments yields both names."""
        body = BUGGY_PLAYBOOK.replace("probe_exercise", "probe_other")
        repo = _fixture_repo(
            tmp_path, body, '-e "probe_exercise=false probe_other=false"'
        )
        assert [name for _, name, _ in unbooled_conditionals(repo)] == ["probe_other"]

    def test_a_value_with_a_space_keeps_its_name(self, tmp_path):
        found = extra_vars_by_playbook(
            _fixture_repo(tmp_path, BUGGY_PLAYBOOK, '-e "probe_exercise=not here"')
        )
        assert found == {"playbooks/probe.yml": {"probe_exercise"}}

    def test_a_trailing_bare_flag_is_reported_unreadable(self, tmp_path):
        """`-e` with no value sets nothing this gate can name."""
        repo = _fixture_repo(tmp_path, BUGGY_PLAYBOOK, "-e")
        assert unreadable_extra_vars(repo) == [(".gitlab-ci.yml", "-e")]

    @pytest.mark.parametrize("args", ["-e @extra.json", "--extra-vars=@extra.json"])
    def test_a_file_valued_extra_var_is_reported_unreadable(self, tmp_path, args):
        repo = _fixture_repo(tmp_path, BUGGY_PLAYBOOK, args)
        assert unreadable_extra_vars(repo) == [(".gitlab-ci.yml", "@extra.json")]
        assert extra_vars_by_playbook(repo) == {}


class TestRoleWalk:
    """A role is reachable code: its conditionals are checked like a playbook's."""

    def test_a_string_typed_role_conditional_is_reported(self, tmp_path):
        repo = _fixture_repo(tmp_path, ROLE_PLAYBOOK)
        _fixture_role(repo, TASK_FILE.format(suffix=""))
        assert unbooled_conditionals(repo) == [
            (
                "ansible/roles/probe_role/tasks/main.yml",
                "probe_exercise",
                "probe_exercise | default(true)",
            ),
        ]

    def test_a_coerced_role_conditional_passes(self, tmp_path):
        repo = _fixture_repo(tmp_path, ROLE_PLAYBOOK)
        _fixture_role(repo, TASK_FILE.format(suffix=" | bool"))
        assert unbooled_conditionals(repo) == []

    def test_a_role_handler_is_walked(self, tmp_path):
        repo = _fixture_repo(tmp_path, ROLE_PLAYBOOK)
        _fixture_role(repo, TASK_FILE.format(suffix=" | bool"))
        _fixture_role(repo, TASK_FILE.format(suffix=""), part="handlers")
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/roles/probe_role/handlers/main.yml",
        ]

    def test_an_included_role_is_walked(self, tmp_path):
        body = """---
- name: Probe
  hosts: all
  tasks:
    - name: Apply the role
      ansible.builtin.include_role:
        name: probe_role
"""
        repo = _fixture_repo(tmp_path, body)
        _fixture_role(repo, TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/roles/probe_role/tasks/main.yml",
        ]

    def test_a_meta_dependency_is_walked(self, tmp_path):
        repo = _fixture_repo(tmp_path, ROLE_PLAYBOOK)
        _fixture_role(repo, TASK_FILE.format(suffix=" | bool"))
        meta = repo / "ansible/roles/probe_role/meta"
        meta.mkdir(parents=True)
        (meta / "main.yml").write_text("---\ndependencies:\n  - role: probe_dep\n")
        dependency = repo / "ansible/roles/probe_dep/tasks"
        dependency.mkdir(parents=True)
        (dependency / "main.yml").write_text(TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/roles/probe_dep/tasks/main.yml",
        ]

    def test_an_unresolvable_role_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(
            tmp_path, ROLE_PLAYBOOK.replace("probe_role", f"{COLLECTION}.no_such_role")
        )
        with pytest.raises(Uninspectable, match="no_such_role"):
            unbooled_conditionals(repo)

    def test_a_role_directory_without_content_is_not_a_role(self, tmp_path):
        """`ansible/roles/<name>` holding only molecule scratch must not resolve."""
        repo = _fixture_repo(tmp_path, ROLE_PLAYBOOK)
        (repo / "ansible/roles/probe_role/.ansible/roles").mkdir(parents=True)
        with pytest.raises(Uninspectable, match="probe_role"):
            unbooled_conditionals(repo)

    def test_an_unparseable_reachable_file_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(tmp_path, ROLE_PLAYBOOK)
        _fixture_role(repo, "---\n- name: broken\n  when: [\n")
        with pytest.raises(Uninspectable, match="does not parse"):
            unbooled_conditionals(repo)


INCLUDE_PLAYBOOK = """---
- name: Probe
  hosts: all
  tasks:
    - name: Run the backend steps
      ansible.builtin.include_tasks: "{target}"
"""


class TestFailClosedWalk:
    """Every edge the walk cannot follow raises: a narrowed set reads as a pass."""

    def test_a_templated_include_inspects_every_candidate(self, tmp_path):
        """The one convention that bends the rule: `disks-{{ x }}.yml` globs to
        `disks-*.yml`, and all of them are checked."""
        repo = _fixture_repo(
            tmp_path, INCLUDE_PLAYBOOK.format(target="disks-{{ probe_backend }}.yml")
        )
        playbooks = repo / "ansible/playbooks"
        (playbooks / "disks-zfs.yml").write_text(TASK_FILE.format(suffix=" | bool"))
        (playbooks / "disks-lvm.yml").write_text(TASK_FILE.format(suffix=""))
        reached = {p.name for p in reachable_files("playbooks/probe.yml", repo)}
        assert {"disks-zfs.yml", "disks-lvm.yml"} <= reached, reached
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/playbooks/disks-lvm.yml",
        ]

    def test_a_templated_include_matching_nothing_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(
            tmp_path, INCLUDE_PLAYBOOK.format(target="{{ probe_backend }}-steps.yml")
        )
        with pytest.raises(Uninspectable, match=r"probe.yml: templated include"):
            unbooled_conditionals(repo)

    def test_a_templated_absolute_include_fails_the_gate(self, tmp_path):
        """An absolute pattern is one `Path.glob` refuses, not one that matched."""
        repo = _fixture_repo(
            tmp_path, INCLUDE_PLAYBOOK.format(target="/opt/{{ probe_backend }}.yml")
        )
        with pytest.raises(Uninspectable, match="templated include target"):
            unbooled_conditionals(repo)

    def test_a_wholly_templated_include_inspects_every_sibling(self, tmp_path):
        """`{{ x }}` globs to `*`: over-approximating is the conservative read."""
        repo = _fixture_repo(
            tmp_path, INCLUDE_PLAYBOOK.format(target="{{ probe_file }}")
        )
        (repo / "ansible/playbooks/steps.yml").write_text(TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/playbooks/steps.yml",
        ]

    def test_an_unresolvable_include_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(tmp_path, INCLUDE_PLAYBOOK.format(target="_missing.yml"))
        with pytest.raises(Uninspectable, match="_missing.yml` resolves to no file"):
            unbooled_conditionals(repo)

    def test_an_include_naming_no_file_fails_the_gate(self, tmp_path):
        """The dict form without `file:`, which carries no readable target."""
        body = """---
- name: Probe
  hosts: all
  tasks:
    - name: Run something
      ansible.builtin.include_tasks:
        apply:
          become: true
"""
        with pytest.raises(Uninspectable, match="names no file this gate can read"):
            unbooled_conditionals(_fixture_repo(tmp_path, body))

    def test_an_unresolvable_playbook_fails_the_gate(self, tmp_path):
        """A caller naming a playbook this gate cannot find checked nothing."""
        repo = _fixture_repo(tmp_path, BUGGY_PLAYBOOK)
        (repo / ".gitlab-ci.yml").write_text(
            CI_CALL.format(args="-e probe_exercise=false").replace(
                "playbooks/probe.yml", "playbooks/gone.yml"
            )
        )
        with pytest.raises(
            Uninspectable, match="playbooks/gone.yml resolves to no file"
        ):
            unbooled_conditionals(repo)

    def test_a_templated_role_name_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(
            tmp_path, ROLE_PLAYBOOK.replace("probe_role", '"{{ probe_role_name }}"')
        )
        with pytest.raises(Uninspectable, match="templated role"):
            unbooled_conditionals(repo)

    def test_a_role_entry_naming_no_role_fails_the_gate(self, tmp_path):
        body = """---
- name: Probe
  hosts: all
  roles:
    - tags:
        - probe
"""
        with pytest.raises(Uninspectable, match="names no role"):
            unbooled_conditionals(_fixture_repo(tmp_path, body))


ACTION_TASK = """---
- name: Probe
  hosts: all
  tasks:
    - name: Run the backend steps
{body}
"""


def _action_playbook(body: str) -> str:
    """A one-task playbook whose task is written in the `action:` form."""
    return ACTION_TASK.format(body="\n".join(f"      {line}" for line in body))


class TestActionFormWalk:
    """`action:`/`local_action:` hide the module in the value, so the key-form
    walk never sees the edge. Each is resolved, or fails the gate."""

    @pytest.mark.parametrize("key", ["action", "local_action"])
    def test_a_free_form_include_is_resolved(self, tmp_path, key):
        repo = _fixture_repo(
            tmp_path, _action_playbook([f"{key}: include_tasks steps.yml"])
        )
        (repo / "ansible/playbooks/steps.yml").write_text(TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/playbooks/steps.yml",
        ]

    def test_a_free_form_include_with_a_named_file_is_resolved(self, tmp_path):
        repo = _fixture_repo(
            tmp_path, _action_playbook(["action: include_tasks file=steps.yml"])
        )
        (repo / "ansible/playbooks/steps.yml").write_text(TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/playbooks/steps.yml",
        ]

    def test_a_positional_target_survives_a_keyword_parameter(self, tmp_path):
        """`include_tasks steps.yml apply=...` keeps naming the file it runs."""
        repo = _fixture_repo(
            tmp_path,
            _action_playbook(["action: include_tasks steps.yml tags=probe"]),
        )
        (repo / "ansible/playbooks/steps.yml").write_text(TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/playbooks/steps.yml",
        ]

    def test_a_dict_form_include_is_resolved(self, tmp_path):
        repo = _fixture_repo(
            tmp_path,
            _action_playbook(
                ["action:", "  module: import_tasks", "  file: steps.yml"]
            ),
        )
        (repo / "ansible/playbooks/steps.yml").write_text(TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/playbooks/steps.yml",
        ]

    def test_a_templated_include_target_globs_like_the_key_form(self, tmp_path):
        repo = _fixture_repo(
            tmp_path,
            _action_playbook(["action: include_tasks disks-{{ probe_backend }}.yml"]),
        )
        playbooks = repo / "ansible/playbooks"
        (playbooks / "disks-zfs.yml").write_text(TASK_FILE.format(suffix=" | bool"))
        (playbooks / "disks-lvm.yml").write_text(TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/playbooks/disks-lvm.yml",
        ]

    @pytest.mark.parametrize(
        "body",
        [
            ["action: import_role probe_role"],
            ["action: include_role name=probe_role"],
            ["action:", "  module: import_role", "  name: probe_role"],
        ],
    )
    def test_a_role_invoked_through_the_action_form_is_walked(self, tmp_path, body):
        repo = _fixture_repo(tmp_path, _action_playbook(body))
        _fixture_role(repo, TASK_FILE.format(suffix=""))
        assert [f for f, _, _ in unbooled_conditionals(repo)] == [
            "ansible/roles/probe_role/tasks/main.yml",
        ]

    def test_an_include_with_no_readable_target_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(
            tmp_path, _action_playbook(["action:", "  module: include_tasks"])
        )
        with pytest.raises(Uninspectable, match="no plain-string target"):
            unbooled_conditionals(repo)

    def test_a_role_with_no_readable_name_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(
            tmp_path,
            _action_playbook(
                ["action:", "  module: include_role", "  apply:", "    become: true"]
            ),
        )
        with pytest.raises(Uninspectable, match="no plain-string target"):
            unbooled_conditionals(repo)

    def test_a_templated_role_name_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(
            tmp_path,
            _action_playbook(
                ["action:", "  module: import_role", '  name: "{{ probe_role_name }}"']
            ),
        )
        with pytest.raises(Uninspectable, match="templated role"):
            unbooled_conditionals(repo)

    def test_an_unresolvable_include_target_fails_the_gate(self, tmp_path):
        repo = _fixture_repo(
            tmp_path, _action_playbook(["action: include_tasks _missing.yml"])
        )
        with pytest.raises(Uninspectable, match="_missing.yml` resolves to no file"):
            unbooled_conditionals(repo)

    def test_an_action_naming_another_module_is_left_alone(self, tmp_path):
        """Only include, import and role modules are reachability edges."""
        repo = _fixture_repo(
            tmp_path, _action_playbook(["action: ansible.builtin.command /bin/true"])
        )
        assert unbooled_conditionals(repo) == []
