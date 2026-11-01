#!/usr/bin/env python3
"""Report role defaults that change between two weisssrv.infra refs.

Default-guarded role variables flip with no inventory edit; run at a pin bump.
Exit 0 clean, 1 unadopted or partly declared flips, 2 a ref or inventory unreadable.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("ERROR: PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import inventory_tree  # noqa: E402  (resolved from this script's own directory)
except ImportError as exc:  # pragma: no cover - environment guard
    print(
        f"ERROR: {exc.name or 'inventory_tree'}.py must sit next to this script "
        "- vendor it (see weisssrv-lib scripts/vendorable-paths.yml).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parent.parent
COLLECTION = "ansible_collections/weisssrv/infra"
INVENTORY = REPO / "ansible/inventories/prod"
PLAYBOOKS = REPO / "ansible/playbooks"
DEFAULTS_FILES = ("defaults/main.yml", "defaults/main.yaml")
DEFAULTS_DIR = "defaults/main/"
META_FILES = ("meta/main.yml", "meta/main.yaml")
ROLE_TASKS = (
    "include_role", "import_role", "ansible.builtin.include_role", "ansible.builtin.import_role",
)
TASK_FILES = (
    "include_tasks", "import_tasks", "ansible.builtin.include_tasks", "ansible.builtin.import_tasks",
)
TASK_LISTS = ("pre_tasks", "tasks", "post_tasks", "handlers")
BLOCK_KEYS = ("block", "rescue", "always")
YAML_SUFFIXES = (".yml", ".yaml")


class OperatorError(Exception):
    """The gate could not inspect its subject: exit 2, never a silent pass."""


def lib_root(explicit: str | None = None, collection: str = COLLECTION) -> Path:
    path = Path(explicit or os.environ.get("WEISSSRV_LIB_PATH") or REPO.parent / "weisssrv-lib")
    if not (path / collection).is_dir():
        raise OperatorError(f"{path} carries no {collection} collection")
    return path


class _Removed:
    """The value a default takes on the side of a ref that no longer ships it."""

    def __repr__(self) -> str:
        return "<removed>"


REMOVED = _Removed()


def _git(lib: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(lib), *args], capture_output=True, text=True, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperatorError(f"git {' '.join(args)} in {lib} could not run: {exc}") from exc
    if done.returncode != 0:
        raise OperatorError(f"git {' '.join(args)} in {lib}: {done.stderr.strip()}")
    return done.stdout


def _role_files(lib: Path, ref: str, collection: str) -> list[tuple[str, str, str]]:
    """(role, path inside the role, repo path) for every file the ref ships under roles/."""
    roles = f"{collection}/roles"
    listing = _git(lib, "ls-tree", "-r", "--name-only", ref, "--", f"{roles}/")
    out = []
    for line in listing.splitlines():
        rest = line[len(roles) + 1:] if line.startswith(f"{roles}/") else ""
        role, _, tail = rest.partition("/")
        if role and tail:
            out.append((role, tail, line))
    return out


def _show_mapping(lib: Path, ref: str, relpath: str, what: str) -> dict:
    text = _git(lib, "show", f"{ref}:{relpath}")
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise OperatorError(f"{ref}:{relpath}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise OperatorError(f"{ref}:{relpath}: {what} are not a mapping")
    return parsed


def role_defaults(lib: Path, ref: str, collection: str = COLLECTION) -> dict[str, dict]:
    """{role: defaults mapping} for every role the ref ships defaults for.

    Ansible's order picks the source: defaults/main/, then main.yml, then main.yaml.
    A directory's files merge in path order; an unparseable file is a fault.
    """
    single: dict[str, dict[str, str]] = {}
    split: dict[str, list[str]] = {}
    for role, tail, line in _role_files(lib, ref, collection):
        if tail in DEFAULTS_FILES:
            single.setdefault(role, {})[tail] = line
        elif tail.startswith(DEFAULTS_DIR) and tail.endswith(YAML_SUFFIXES):
            split.setdefault(role, []).append(line)
    wanted = {
        role: [next(files[name] for name in DEFAULTS_FILES if name in files)]
        for role, files in single.items()
    }
    wanted.update({role: sorted(lines, key=lambda p: p.split("/")) for role, lines in split.items()})
    out: dict[str, dict] = {}
    for role, relpaths in sorted(wanted.items()):
        merged: dict = {}
        for relpath in relpaths:
            merged.update(_show_mapping(lib, ref, relpath, "defaults"))
        out[role] = merged
    if not out:
        raise OperatorError(f"{ref} ships no role defaults: nothing would be compared")
    return out


def role_dependencies(lib: Path, ref: str, collection: str = COLLECTION) -> dict[str, set[str]]:
    """{role: collection roles its meta dependencies pull in}, short names."""
    prefix = collection_prefix(collection)
    out: dict[str, set[str]] = {}
    for role, tail, line in _role_files(lib, ref, collection):
        if tail not in META_FILES:
            continue
        deps = _show_mapping(lib, ref, line, "meta").get("dependencies") or []
        if not isinstance(deps, list):
            raise OperatorError(f"{ref}:{line}: dependencies are not a list")
        for dep in deps:
            name = dep if isinstance(dep, str) else (
                (dep.get("role") or dep.get("name")) if isinstance(dep, dict) else None
            )
            if isinstance(name, str) and "{{" not in name:
                out.setdefault(role, set()).add(name.removeprefix(prefix))
    return out


def collection_prefix(collection: str) -> str:
    """`ansible_collections/weisssrv/infra` -> `weisssrv.infra.`, the FQCN prefix."""
    namespace, name = collection.rstrip("/").split("/")[-2:]
    return f"{namespace}.{name}."


def flipped_defaults(
    old: dict[str, dict], new: dict[str, dict]
) -> list[tuple[str, str, object, object]]:
    """(role, key, old, new) for every default of a role the new ref ships that changes.

    Null, type and mapping changes count, and a key the new ref drops flips to
    REMOVED. A key only the new ref ships does not count.
    """
    flips = []
    for role, after in sorted(new.items()):
        before = old.get(role) or {}
        for key in sorted(before.keys() | after.keys(), key=str):
            if key not in before:
                continue
            was, value = before[key], after.get(key, REMOVED)
            if was != value or type(was) is not type(value):
                flips.append((role, key, was, value))
    return flips


def describe(was: object, now: object) -> str:
    """One readable change: a mapping names its changed keys, not its whole value."""
    if now is REMOVED:
        return "is removed from the role defaults, so the task's inline default applies"
    if isinstance(was, dict) and isinstance(now, dict):
        changed = sorted(
            str(k) for k in was.keys() | now.keys()
            if k not in was or k not in now or was[k] != now[k]
            or type(was[k]) is not type(now[k])
        )
        return f"mapping changes at {', '.join(changed)}"
    return f"flips {was!r} -> {now!r}"


def _load(path: Path):
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OperatorError(f"{path}: {exc}") from exc


def _vars_files(base: Path) -> list[tuple[str, Path]]:
    """(scope name, file) for `<name>.yml` files and `<name>/*.yml` directories."""
    found: list[tuple[str, Path]] = []
    if not base.is_dir():
        return found
    for entry in sorted(base.iterdir()):
        if entry.is_file() and entry.suffix in YAML_SUFFIXES:
            found.append((entry.stem, entry))
        elif entry.is_dir():
            found += [
                (entry.name, f)
                for f in sorted(entry.rglob("*"))
                if f.is_file() and f.suffix in YAML_SUFFIXES
            ]
    return found


class Declarations:
    """Where an inventory declares each variable, and which hosts each scope reaches."""

    def __init__(self) -> None:
        self.scopes: dict[str, set[tuple[str, str]]] = {}
        self.index: dict[str, dict] = {}
        self.play_cover: dict[str, dict[str, set[str]]] = {}

    def cover(self, key: str, role: str, hosts: set[str]) -> None:
        """A playbook passes `key` to `role` on `hosts`."""
        self.play_cover.setdefault(key, {}).setdefault(role, set()).update(hosts)

    def add(self, key: object, kind: str, name: str) -> None:
        self.scopes.setdefault(str(key), set()).add((kind, str(name)))

    def merge_index(self, inventory: dict) -> None:
        for group, entry in inventory_tree.group_index(inventory).items():
            merged = self.index.setdefault(group, {"hosts": [], "children": []})
            for field in ("hosts", "children"):
                merged[field] += [v for v in entry[field] if v not in merged[field]]

    def all_hosts(self) -> set[str]:
        return set(self._resolve("all"))

    def _resolve(self, group: str) -> list[str]:
        try:
            return inventory_tree.resolve_hosts(group, self.index, strict=True)
        except inventory_tree.InventoryCycle as exc:
            raise OperatorError(str(exc)) from exc

    def declares(self, role: str, key: str) -> bool:
        """The inventory declares `key`, or a playbook passes it to `role`."""
        return key in self.scopes or role in self.play_cover.get(key, {})

    def uncovered(
        self, key: str, targets: set[str] | None = None, role: str | None = None
    ) -> list[str]:
        """Hosts of `targets` (default every host) no declaration of `key` reaches.

        Playbook declarations count only for `role`, or for any role when it is None.
        """
        scopes = self.scopes.get(key, set())
        if ("group", "all") in scopes:
            return []
        passed = self.play_cover.get(key, {})
        covered: set[str] = set().union(
            *(hosts for name, hosts in passed.items() if role is None or name == role)
        )
        for kind, name in scopes:
            covered.update(self._resolve(name) if kind == "group" else [name])
        return sorted((self.all_hosts() if targets is None else targets) - covered)

    def pattern_hosts(self, pattern) -> set[str] | None:
        """Hosts a play pattern reaches, None when it cannot be read statically.

        `!` and `&` terms only narrow a play, so they are dropped: the wider set
        is the strict reading for targets. `localhost` stands for itself.
        """
        if not isinstance(pattern, str | list) or "{{" in str(pattern):
            return None
        every = self.all_hosts()
        hosts: set[str] = set()
        for token in _pattern_terms(pattern):
            if not token or token[0] in "!&":
                continue
            if token in self.index:
                hosts.update(self._resolve(token))
            elif token in every or token == "localhost":
                hosts.add(token)
            else:
                return None
        return hosts


def _pattern_terms(pattern: str | list) -> list[str]:
    """The terms of a host pattern, with any subscript stripped."""
    if isinstance(pattern, list):
        pattern = ",".join(str(p) for p in pattern)
    return [re.sub(r"\[.*\]$", "", t.strip()) for t in re.split(r"[:,]", pattern)]


def narrows(pattern) -> bool:
    """A `!` or `&` term excludes hosts, so the play's vars cover none for sure."""
    return isinstance(pattern, str | list) and any(
        t[:1] in ("!", "&") for t in _pattern_terms(pattern)
    )


def _mapping(value, where: str) -> dict:
    """An inventory mapping, empty when null; any other shape is a fault."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise OperatorError(f"{where} is a {type(value).__name__}, not a mapping")
    return value


def _walk_inventory(decl: Declarations, name: str, defn) -> None:
    """Record every `vars:` mapping and every inline host mapping under a group."""
    if not isinstance(defn, dict):
        return
    for key in _mapping(defn.get("vars"), f"group {name} vars"):
        decl.add(key, "group", name)
    for host, hostvars in _mapping(defn.get("hosts"), f"group {name} hosts").items():
        for key in _mapping(hostvars, f"host {host} in group {name}"):
            decl.add(key, "host", host)
    for child, child_defn in _mapping(defn.get("children"), f"group {name} children").items():
        _walk_inventory(decl, str(child), child_defn)


def inventory_declarations(inventory: Path) -> Declarations:
    """Every variable the inventory declares, with the group or host scope declaring it."""
    decl = Declarations()
    if not inventory.is_dir():
        raise OperatorError(f"{inventory} is not an inventory directory")
    for path in sorted(inventory.iterdir()):
        if not (path.is_file() and path.suffix in YAML_SUFFIXES):
            continue
        parsed = _load(path)
        if isinstance(parsed, dict) and isinstance(parsed.get("all"), dict):
            _walk_inventory(decl, "all", parsed["all"])
            decl.merge_index(parsed)
    for kind, sub in (("group", "group_vars"), ("host", "host_vars")):
        for name, path in _vars_files(inventory / sub):
            parsed = _load(path)
            if isinstance(parsed, dict):
                for key in parsed:
                    decl.add(key, kind, name)
    if not decl.scopes:
        raise OperatorError(f"{inventory} declares no variables: the gate has nothing to check")
    if not decl.all_hosts():
        raise OperatorError(
            f"no host index read from {inventory}: partial coverage cannot be computed"
        )
    return decl


def _mapping_keys(value) -> set[str]:
    return {str(k) for k in value} if isinstance(value, dict) else set()


class _Walk:
    """Task-file walk state shared by every play: visited files, opaque includes."""

    def __init__(self) -> None:
        self.visited: set[Path] = set()
        self.opaque = False

    def tasks(self, tasks, base: Path, seen: tuple[Path, ...]) -> dict[str, set[str]]:
        """{role: keys its `vars:` pass} for a task list, through static includes."""
        roles: dict[str, set[str]] = {}
        for task in tasks if isinstance(tasks, list) else []:
            if not isinstance(task, dict):
                continue
            for key in BLOCK_KEYS:
                _merge(roles, self.tasks(task.get(key), base, seen))
            for key in ROLE_TASKS:
                spec = task.get(key)
                name = spec.get("name") if isinstance(spec, dict) else spec
                if isinstance(name, str):
                    _merge(roles, {name: _mapping_keys(task.get("vars"))})
            for key in TASK_FILES:
                spec = task.get(key)
                target = spec.get("file") if isinstance(spec, dict) else spec
                if not isinstance(target, str):
                    continue
                path = (base / target).resolve()
                if "{{" in target or not path.is_file():
                    self.opaque = True
                    continue
                if path in seen:
                    continue
                self.visited.add(path)
                _merge(roles, self.tasks(_load(path), path.parent, (*seen, path)))
        return roles


def _merge(into: dict[str, set[str]], more: dict[str, set[str]]) -> None:
    for role, keys in more.items():
        into.setdefault(role, set()).update(keys)


def _closure(roles: set[str], dependencies: dict[str, set[str]]) -> set[str]:
    """`roles` and every role their meta dependencies pull in, transitively."""
    out, todo = set(roles), list(roles)
    while todo:
        for dep in dependencies.get(todo.pop(), set()) - out:
            out.add(dep)
            todo.append(dep)
    return out


def _play_keys(play: dict, base: Path) -> set[str]:
    """Variables the play itself declares, through `vars:` and literal `vars_files`."""
    keys = _mapping_keys(play.get("vars"))
    files = play.get("vars_files") or []
    for entry in files if isinstance(files, list) else [files]:
        if isinstance(entry, str) and "{{" not in entry and (base / entry).is_file():
            keys |= _mapping_keys(_load(base / entry))
    return keys


def role_targets(
    playbooks: Path,
    decl: Declarations,
    collection: str = COLLECTION,
    dependencies: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    """{role: hosts the playbooks run it on}, short names, meta dependencies included.

    An unplaceable role targets every host. Play vars cover the play's roles and
    their dependencies, role vars that role only, and a narrowed play covers none.
    """
    if not playbooks.is_dir():
        raise OperatorError(f"{playbooks} is not a playbooks directory")
    prefix = collection_prefix(collection)
    mention = re.compile(rf"{re.escape(prefix)}(\w+)")
    walk = _Walk()
    targets: dict[str, set[str]] = {}
    unplaced: set[str] = set()
    task_files: dict[Path, set[str]] = {}
    plays = 0
    for path in sorted(f for f in playbooks.rglob("*") if f.is_file() and f.suffix in YAML_SUFFIXES):
        parsed = _load(path)
        mentioned = set(mention.findall(path.read_text(encoding="utf-8")))
        entries = [p for p in parsed if isinstance(p, dict)] if isinstance(parsed, list) else []
        if not any("hosts" in p or "import_playbook" in p or "ansible.builtin.import_playbook" in p
                   for p in entries):
            task_files[path.resolve()] = mentioned
            continue
        for play in entries:
            if "hosts" not in play:
                continue
            plays += 1
            roles: dict[str, set[str]] = {}
            listed = play.get("roles") or []
            for entry in listed if isinstance(listed, list) else []:
                name = entry if isinstance(entry, str) else (
                    entry.get("role") if isinstance(entry, dict) else None
                )
                if isinstance(name, str):
                    passed = entry.get("vars") if isinstance(entry, dict) else None
                    _merge(roles, {name: _mapping_keys(passed)})
            for key in TASK_LISTS:
                _merge(roles, walk.tasks(play.get(key), path.parent, (path.resolve(),)))
            short = {
                r.removeprefix(prefix): keys for r, keys in roles.items() if r.startswith(prefix)
            }
            hosts = decl.pattern_hosts(play["hosts"])
            if hosts is None:
                unplaced |= set(short)
                continue
            for role in short:
                targets.setdefault(role, set()).update(hosts)
            if narrows(play["hosts"]):
                continue
            play_keys = _play_keys(play, path.parent)
            for role in _closure(set(short), dependencies or {}):
                for key in play_keys | short.get(role, set()):
                    decl.cover(key, role, hosts)
    if not plays:
        raise OperatorError(f"{playbooks} holds no plays: role targets cannot be computed")
    for path, mentioned in task_files.items():
        if walk.opaque or path not in walk.visited:
            unplaced |= mentioned
    for role in unplaced:
        targets[role] = set(decl.all_hosts())
    _spread_dependencies(targets, dependencies or {})
    return targets


def _spread_dependencies(targets: dict[str, set[str]], dependencies: dict[str, set[str]]) -> None:
    """A role's meta dependencies run on every host the role runs on."""
    changed = True
    while changed:
        changed = False
        for role, deps in dependencies.items():
            for dep in deps:
                before = len(targets.get(dep, set()))
                targets.setdefault(dep, set()).update(targets.get(role, set()))
                changed = changed or len(targets[dep]) != before


def unadopted(
    flips: list[tuple[str, str, object, object]], decl: Declarations, allowed: set[str]
) -> list[str]:
    return [
        f"{role}: {key} {describe(was, now)} and the inventory declares neither "
        "it nor an override"
        for role, key, was, now in flips
        if not decl.declares(role, key) and key not in allowed
    ]


def partial(
    flips: list[tuple[str, str, object, object]],
    decl: Declarations,
    allowed: set[str],
    targets: dict[str, set[str]] | None = None,
) -> list[str]:
    """Flips declared for some of the role's hosts only: the rest take the new default.

    `targets` maps each role to the hosts it runs on; a role no play runs is
    skipped. Without `targets`, every inventory host counts.
    """
    notes = []
    for role, key, was, now in flips:
        if key in allowed or not decl.declares(role, key):
            continue
        if targets is not None and role not in targets:
            continue
        rest = decl.uncovered(key, None if targets is None else targets[role], role)
        if rest:
            notes.append(
                f"{role}: {key} {describe(was, now)}; no declaration reaches "
                f"{', '.join(rest)}"
            )
    return notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="old", required=True, metavar="REF", help="installed tag")
    ap.add_argument("--to", dest="new", required=True, metavar="REF", help="new tag")
    ap.add_argument("--lib", default=None, help="weisssrv-lib checkout (default $WEISSSRV_LIB_PATH)")
    ap.add_argument("--collection", default=COLLECTION, help="collection path inside the checkout")
    ap.add_argument("--inventory", type=Path, default=INVENTORY)
    ap.add_argument("--playbooks", type=Path, default=PLAYBOOKS)
    ap.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="KEY",
        help="accept this flip without an inventory entry; repeatable",
    )
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="report, but do not fail on, a flip declared for some hosts only",
    )
    args = ap.parse_args(argv)
    allowed = set(args.allow)

    try:
        lib = lib_root(args.lib, args.collection)
        flips = flipped_defaults(
            role_defaults(lib, args.old, args.collection),
            role_defaults(lib, args.new, args.collection),
        )
        decl = inventory_declarations(args.inventory)
        targets = role_targets(
            args.playbooks,
            decl,
            args.collection,
            role_dependencies(lib, args.new, args.collection),
        )
        problems = unadopted(flips, decl, allowed)
        partials = partial(flips, decl, allowed, targets)
    except OperatorError as exc:
        print(f"check-role-default-flips inspected nothing: {exc}", file=sys.stderr)
        return 2

    if partials:
        print("Flipped defaults declared for some hosts only:", file=sys.stderr)
        for note in partials:
            print(f"  {note}", file=sys.stderr)
    if problems:
        print(f"Role defaults flip between {args.old} and {args.new}:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
    if problems or partials:
        print(
            "    Fix: declare the variable in the inventory with the value this "
            "cluster wants, or pass --allow KEY once the new default is adopted "
            "deliberately.",
            file=sys.stderr,
        )
    if problems or (partials and not args.allow_partial):
        return 1
    if partials:
        print(
            f"No undeclared default flips between {args.old} and {args.new}: "
            f"{len(flips)} flip(s), {len(partials)} declared for some hosts only "
            "and accepted by --allow-partial."
        )
        return 0
    print(
        f"No unadopted default flips between {args.old} and {args.new} "
        f"({len(flips)} flip(s) declared in the inventory or allowed)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
