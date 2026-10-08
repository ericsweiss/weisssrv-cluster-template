#!/usr/bin/env python3
"""Check an Ansible inventory against the collection's role conventions.

Catches an opt-in role invoked with its flag set nowhere, and a required input
with no default and no assignment. Static read; contract: docs/SCRIPTS.md.
"""
from __future__ import annotations

import argparse
import ast
import functools
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

try:
    import jinja2
    from jinja2 import meta as jinja_meta
except ImportError:
    print("ERROR: Jinja2 required: pip install jinja2", file=sys.stderr)
    raise SystemExit(2) from None

_ASSIGNMENT = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*:", re.MULTILINE)
# `<var> | default('') | length > 0` — the shape a role uses for "required".
_ASSERTED_NONEMPTY = re.compile(
    r"\b([a-z_][a-z0-9_]*)\s*\|\s*default\(\s*(?:''|\"\"|\[\]|\{\})\s*\)"
    r"\s*\|\s*length\s*>\s*0"
)
_TRUTHY_STRINGS = {"y", "yes", "on", "1", "true", "t"}
_UNMODELLED = object()


class OperatorError(Exception):
    """A bad invocation or a scan that inspected nothing: exit 2, not a finding."""


def parse_allow_disabled(values: List[str]) -> Dict[str, str]:
    """`ROLE=REASON` pairs. A reason is mandatory: an unexplained exemption is
    a hole."""
    allowed: Dict[str, str] = {}
    for raw in values or []:
        role, sep, reason = raw.partition("=")
        if not sep or not role.strip() or not reason.strip():
            raise OperatorError("--allow-disabled takes ROLE=REASON, got %r" % raw)
        allowed[role.strip()] = reason.strip()
    return allowed


def _ansible_bool(value) -> bool:
    """Ansible's `| bool` (boolean(..., strict=False)), which plain Jinja lacks.
    Anything outside the truthy set reads false, as it does in a role's own
    `<flag> | bool` gating — so an unrecognised default is unreachable here too."""
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return bool(value)


def _ansible_regex_test(search: bool):
    """Ansible's `is match` / `is search` tests, which plain Jinja lacks.

    Roles use them in `when:` clauses and in default expressions, so without
    them those expressions read as unmodelled and their inputs go unjudged.
    """
    def test(value, pattern, ignorecase=False, multiline=False) -> bool:
        flags = (re.IGNORECASE if ignorecase else 0) | (re.MULTILINE if multiline else 0)
        probe = re.search if search else re.match
        return bool(probe(str(pattern), str(value), flags))

    return test


@functools.lru_cache(maxsize=1)
def _env() -> "jinja2.Environment":
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)  # noqa: S701
    env.filters["bool"] = _ansible_bool
    env.tests["match"] = _ansible_regex_test(search=False)
    env.tests["search"] = _ansible_regex_test(search=True)
    return env


def _note_unmodelled(unmodelled: Optional[List[str]], message: str) -> None:
    """Record an expression this evaluator could not read, so a dropped input is
    reported rather than silently treated as satisfied."""
    if unmodelled is not None:
        unmodelled.append(message)


class _Loader(yaml.SafeLoader):
    """SafeLoader that tolerates Ansible's `!vault` and friends.

    Not ci_yaml.py's loader: that one is the GitLab pipeline contract, and this
    gate parses Ansible YAML, so the two tag sets stay separate.
    """


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _load(path: Path):
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
    except yaml.YAMLError as exc:
        raise OperatorError("%s: %s" % (path, exc)) from exc


def assigned_names(inventory: Path) -> Set[str]:
    """Every variable name assigned anywhere under the inventory."""
    names: Set[str] = set()
    for path in sorted(inventory.rglob("*.yml")) + sorted(inventory.rglob("*.yaml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            names.update(_ASSIGNMENT.findall(line))
    return names


def opt_in_roles(roles_dir: Path) -> Set[str]:
    """Roles shipping `<role>_enabled: false` — that default is the contract."""
    found = set()
    for role in sorted(p for p in roles_dir.iterdir() if p.is_dir()):
        doc = _load(role / "defaults" / "main.yml") if (
            role / "defaults" / "main.yml"
        ).is_file() else None
        if isinstance(doc, dict):
            flag = "%s_enabled" % role.name
            if flag in doc and not doc[flag]:
                found.add(role.name)
    return found


def role_invocations(playbooks: Path) -> Iterator[Tuple[Path, str, str]]:
    """(playbook, role, when-clause) for every role a playbook invokes, from
    `roles:` entries and include_role/import_role tasks."""
    def when_text(entry: dict) -> str:
        clause = entry.get("when")
        return " ".join(str(c) for c in clause) if isinstance(clause, list) else str(
            clause or ""
        )

    for path in sorted(playbooks.rglob("*.yml")) + sorted(playbooks.rglob("*.yaml")):
        plays = _load(path)
        for play in plays if isinstance(plays, list) else []:
            if not isinstance(play, dict):
                continue
            for entry in play.get("roles") or []:
                if isinstance(entry, dict):
                    yield path, str(entry.get("role", "")), when_text(entry)
                elif isinstance(entry, str):
                    yield path, entry, ""
            for block in ("pre_tasks", "tasks", "post_tasks"):
                for task in play.get(block) or []:
                    if not isinstance(task, dict):
                        continue
                    for verb in ("include_role", "import_role",
                                 "ansible.builtin.include_role",
                                 "ansible.builtin.import_role"):
                        if isinstance(task.get(verb), dict):
                            yield path, str(task[verb].get("name", "")), when_text(task)


def _walk_tasks(tasks, inherited: Tuple[str, ...] = ()):
    """(task, accumulated when-conditions), descending into block/rescue/always
    so a `when:` on the enclosing block is not lost — which is where optional
    features are actually gated."""
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        clause = task.get("when")
        conditions = inherited + tuple(
            str(c)
            for c in (clause if isinstance(clause, list) else [clause] if clause else [])
        )
        nested = False
        for key in ("block", "rescue", "always"):
            if key in task:
                nested = True
                for item in _walk_tasks(task[key], conditions):
                    yield item
        if not nested:
            yield task, conditions


def _reachable_by_default(conditions: Tuple[str, ...], defaults: dict,
                          unmodelled: Optional[List[str]] = None,
                          where: str = ""):
    """Would this task run on a host that sets none of the role's inputs?

    None when the expression is not modelled; the caller then treats the input
    as not required, because a false positive breaks every operator's build.
    """
    env = _env()
    for condition in conditions:
        try:
            verdict = env.from_string(
                "{% if " + condition + " %}yes{% else %}no{% endif %}"
            ).render(**defaults)
        except Exception:  # noqa: BLE001 - unmodelled expression, not a failure
            _note_unmodelled(
                unmodelled,
                "%s: when-expression not modelled, its asserts were not scanned: %s"
                % (where, condition),
            )
            return None
        if verdict != "yes":
            return False
    return True


def asserted_inputs(role: Path, defaults: dict,
                    unmodelled: Optional[List[str]] = None) -> Set[str]:
    """Role-prefixed variables the role asserts non-empty on its default path.

    Restricted to `<role>_*` and to asserts reachable with nothing set, since
    an opt-in feature's assert is a contract rather than a requirement.
    """
    found: Set[str] = set()
    tasks_dir = role / "tasks"
    for path in sorted(tasks_dir.rglob("*.yml")) if tasks_dir.is_dir() else []:
        for task, conditions in _walk_tasks(_load(path)):
            spec = task.get("ansible.builtin.assert") or task.get("assert")
            if not isinstance(spec, dict):
                continue
            if _reachable_by_default(
                conditions, defaults, unmodelled=unmodelled, where=str(path)
            ) is not True:
                continue
            that = spec.get("that")
            for clause in that if isinstance(that, list) else [that] if that else []:
                for name in _ASSERTED_NONEMPTY.findall(str(clause)):
                    if name.startswith(role.name + "_"):
                        found.add(name)
    return found


def _render_default(value, context: dict):
    """What a default actually takes on an inventory holding `context`.

    Whole-template results convert back to a native type, as Ansible does.
    Returns `_UNMODELLED` for an expression this evaluator cannot read.
    """
    if not isinstance(value, str) or "{{" not in value:
        return value
    try:
        rendered = _env().from_string(value).render(**context)
    except Exception:  # noqa: BLE001 - unmodelled expression, not a failure
        return _UNMODELLED
    try:
        return ast.literal_eval(rendered)
    except (ValueError, SyntaxError):
        return rendered


def _referenced_names(value) -> Set[str]:
    if not isinstance(value, str) or "{{" not in value:
        return set()
    try:
        return jinja_meta.find_undeclared_variables(_env().parse(value))
    except Exception:  # noqa: BLE001 - unmodelled expression, not a failure
        return set()


def _is_empty(value) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value in (None, [], {}, ())


def _default_gap(defaults: dict, var: str, context: dict,
                 assigned: Set[str], unmodelled: Optional[List[str]] = None,
                 role_name: str = "") -> Optional[str]:
    """None when defaults/main.yml gives `var` a non-empty value ONCE RENDERED
    against this inventory; otherwise the reason it does not."""
    if var not in defaults:
        return "gives it no default in defaults/main.yml"
    raw = defaults[var]
    value = _render_default(raw, context)
    if value is _UNMODELLED:
        _note_unmodelled(
            unmodelled,
            "%s: %s default expression not modelled, so the input was not judged: %r"
            % (role_name, var, raw),
        )
        return None
    if not _is_empty(value):
        return None
    if (_referenced_names(raw) & assigned) - set(context):
        return None
    if isinstance(raw, str) and "{{" in raw:
        return "defaults it to %r, which renders EMPTY against this inventory" % raw
    return "defaults it to %r, which is empty" % raw


def _role_defaults(role: Path) -> dict:
    path = role / "defaults" / "main.yml"
    doc = _load(path) if path.is_file() else {}
    return doc if isinstance(doc, dict) else {}


def check_opt_ins(roles_dir: Path, inventory: Path, playbooks: Path,
                  allow_empty: bool = False, assigned: Set[str] = None,
                  allow_disabled: Dict[str, str] = None) -> List[str]:
    opt_in = opt_in_roles(roles_dir)
    if not opt_in:
        raise OperatorError(
            "no role in %s declares `<role>_enabled: false` — the opt-in "
            "convention this check reads has changed, and it is now examining "
            "nothing" % roles_dir
        )
    assigned = assigned_names(inventory) if assigned is None else assigned
    allow_disabled = allow_disabled or {}
    checked, problems = 0, []
    for path, role, when in role_invocations(playbooks):
        name = role.rsplit(".", 1)[-1]
        if name not in opt_in:
            continue
        checked += 1
        if name in allow_disabled:
            print("  role opt-ins: %s exempt (%s)" % (name, allow_disabled[name]))
            continue
        flag = "%s_enabled" % name
        if flag in assigned or flag in when:
            continue
        problems.append(
            "%s invokes %s unconditionally, but %s is set nowhere in %s — so the "
            "role takes its defaults on every host it touches, which for a role "
            "that reconciles its disabled state is not a no-op"
            % (path, role, flag, inventory)
        )
    if not checked:
        message = (
            "no opt-in role invocation was examined: the playbooks invoke none "
            "of the collection's opt-in roles (%s), or the invocation scan is "
            "stale. A consumer that composes no opt-in role runs "
            "`--skip opt-ins` or `--allow-empty`" % ", ".join(sorted(opt_in))
        )
        if not allow_empty:
            raise OperatorError(message)
        print("  role opt-ins: %s" % message)
    if not problems:
        print("  role opt-ins ok (%d invocations of %d opt-in roles)"
              % (checked, len(opt_in)))
    return problems


def check_required_inputs(roles_dir: Path, inventory: Path, playbooks: Path,
                          allow_empty: bool = False,
                          assigned: Set[str] = None,
                          unmodelled: Optional[List[str]] = None) -> List[str]:
    assigned = assigned_names(inventory) if assigned is None else assigned
    notes: List[str] = [] if unmodelled is None else unmodelled
    # Group vars as VALUES, not just names: a role's feature flag can default
    # true in the collection and false in a site, and an assert's `when:` can
    # only be judged with the inventory's answer in hand.
    group_values: dict = {}
    group_vars = inventory / "group_vars"
    for path in (sorted(group_vars.rglob("*.yml"))
                 + sorted(group_vars.rglob("*.yaml"))):
        doc = _load(path)
        if isinstance(doc, dict):
            group_values.update(doc)

    invoked: Dict[str, Path] = {
        role.rsplit(".", 1)[-1]: path
        for path, role, _when in role_invocations(playbooks)
        if role
    }
    required, problems = 0, []
    for name, playbook in sorted(invoked.items()):
        role = roles_dir / name
        if not role.is_dir():
            continue
        defaults = _role_defaults(role)
        context = dict(defaults)
        context.update(group_values)
        for var in sorted(asserted_inputs(role, context, unmodelled=notes)):
            gap = _default_gap(defaults, var, context, assigned,
                               unmodelled=notes, role_name=name)
            if gap is None:
                continue
            required += 1
            if var in assigned:
                continue
            problems.append(
                "%s invokes %s, which asserts %s and %s — and %s is set nowhere "
                "in %s, so the role's opening assert fails on every host it "
                "touches" % (playbook, name, var, gap, var, inventory)
            )
    for note in sorted(set(notes)):
        print("  required role inputs: UNMODELLED %s" % note)
    if not required:
        message = (
            "no invoked role declares an asserted input without a usable "
            "default: the playbooks compose only roles that default every "
            "asserted input, or the assert scan is stale. A consumer in that "
            "position runs `--skip required-inputs` or `--allow-empty`"
        )
        if not allow_empty:
            raise OperatorError(message)
        print("  required role inputs: %s" % message)
    if not problems:
        print("  required role inputs ok (%d asserted inputs with no usable "
              "default assigned, %d expression(s) not modelled)"
              % (required, len(set(notes))))
    return sorted(set(problems))

_TEXT_SUFFIXES = (".yml", ".yaml", ".j2", ".cfg", ".conf", ".ini", ".py", ".sh", ".service")


def assigned_locations(inventory: Path) -> Dict[str, Set[Path]]:
    """Variable name -> the inventory files that assign it."""
    where: Dict[str, Set[Path]] = {}
    for path in sorted(inventory.rglob("*.yml")) + sorted(inventory.rglob("*.yaml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for name in _ASSIGNMENT.findall(line):
                where.setdefault(name, set()).add(path)
    return where


def consumed_text(roles_dir: Path, playbooks: Path) -> str:
    """Every role and playbook source line a variable could be read from.

    Molecule scenarios and prose are left out: a name that survives only in a
    test fixture or a README is not consumed by anything that runs.
    """
    chunks: List[str] = []
    for root in (roles_dir, playbooks):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            if "molecule" in path.parts:
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
    if not chunks:
        raise OperatorError(
            "no role or playbook source was read under %s / %s — the unknown-inputs "
            "arm would call every inventory variable unknown" % (roles_dir, playbooks)
        )
    return "\n".join(chunks)


def _owning_role(name: str, roles: Set[str]) -> Optional[str]:
    """The longest role name `name` is prefixed with, as `<role>_...`."""
    owners = [r for r in roles if name.startswith(r + "_")]
    return max(owners, key=len) if owners else None


def check_unknown_inputs(roles_dir: Path, inventory: Path, playbooks: Path,
                         allow_empty: bool = False,
                         allow_unknown: Dict[str, str] = None) -> List[str]:
    """Role-prefixed inventory variables no role or playbook reads.

    Role variables are `| default(...)`-guarded, so a typo or a variable the
    pinned collection renamed is inert rather than fatal.
    """
    allow_unknown = allow_unknown or {}
    roles = {p.name for p in roles_dir.iterdir() if p.is_dir()}
    if not roles:
        raise OperatorError("no role directory under %s" % roles_dir)
    text = consumed_text(roles_dir, playbooks)
    locations = assigned_locations(inventory)
    checked, problems = 0, []
    for name in sorted(locations):
        role = _owning_role(name, roles)
        if role is None:
            continue
        checked += 1
        if name in allow_unknown:
            print("  unknown inputs: %s exempt (%s)" % (name, allow_unknown[name]))
            continue
        if re.search(r"\b%s\b" % re.escape(name), text):
            continue
        files = ", ".join(str(f) for f in sorted(locations[name]))
        problems.append(
            "%s sets %s, which the %s role reads nowhere and no playbook "
            "references — the role's variables are `| default(...)`-guarded, so "
            "this value is silently inert. Fix the spelling, adopt the name the "
            "pinned collection uses, or drop the assignment"
            % (files, name, role)
        )
    if not checked:
        message = (
            "no `<role>_`-prefixed inventory variable was examined: the inventory "
            "names none of the collection's roles (%s), or the prefix convention "
            "has changed. A consumer in that position runs `--skip unknown-inputs` "
            "or `--allow-empty`" % ", ".join(sorted(roles))
        )
        if not allow_empty:
            raise OperatorError(message)
        print("  unknown role inputs: %s" % message)
    if not problems:
        print("  unknown role inputs ok (%d role-prefixed variables all consumed)"
              % checked)
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--roles-dir", type=Path, required=True,
        help="the collection's roles/ directory, checkout or installed copy",
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--playbooks", type=Path, required=True)
    parser.add_argument(
        "--skip", action="append", default=[],
        choices=["opt-ins", "required-inputs", "unknown-inputs"],
        help="arm to leave out; repeatable",
    )
    parser.add_argument(
        "--allow-disabled", action="append", default=[], metavar="ROLE=REASON",
        help="opt-in role whose unconditional invocation is intended, because "
             "it reconciles its disabled state; repeatable",
    )
    parser.add_argument(
        "--allow-unknown", action="append", default=[], metavar="VAR=REASON",
        help="role-prefixed inventory variable no role reads on purpose; repeatable",
    )
    parser.add_argument(
        "--allow-empty", action="store_true",
        help="report an arm that found nothing to examine instead of failing, "
             "for a consumer that composes no opt-in role",
    )
    args = parser.parse_args(argv)

    for label, path in (("--roles-dir", args.roles_dir),
                        ("--inventory", args.inventory),
                        ("--playbooks", args.playbooks)):
        if not path.is_dir():
            print("%s: no such directory: %s" % (label, path), file=sys.stderr)
            return 2

    if set(args.skip) >= {"opt-ins", "required-inputs", "unknown-inputs"}:
        print("ERROR: every arm skipped — nothing would be checked", file=sys.stderr)
        return 2

    problems: List[str] = []
    try:
        allow_disabled = parse_allow_disabled(args.allow_disabled)
        assigned = assigned_names(args.inventory)
        if "opt-ins" not in args.skip:
            problems.extend(check_opt_ins(
                args.roles_dir, args.inventory, args.playbooks, args.allow_empty,
                assigned=assigned, allow_disabled=allow_disabled))
        if "required-inputs" not in args.skip:
            problems.extend(check_required_inputs(
                args.roles_dir, args.inventory, args.playbooks, args.allow_empty,
                assigned=assigned))
        if "unknown-inputs" not in args.skip:
            problems.extend(check_unknown_inputs(
                args.roles_dir, args.inventory, args.playbooks, args.allow_empty,
                allow_unknown=parse_allow_disabled(args.allow_unknown)))
    except OperatorError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if problems:
        for problem in problems:
            print("FAIL %s" % problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
