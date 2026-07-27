"""Validate a rendered cluster with the real toolchain.

The pytest suite asserts structure with PyYAML alone; this runs what the
generated repository's own pipeline runs — yamllint over the tree, the Flux
render loop (kustomize build -> postBuild substitution -> kubeconform), and
`ansible-playbook --syntax-check` against the weisssrv.infra collection. With
`--lib-path` it also checks that every vendored script is still byte-identical
to the library's copy at that ref.

    python3 tests/validate_render.py                       # render, then check
    python3 tests/validate_render.py --render-dir /tmp/x   # check an existing render
    python3 tests/validate_render.py --lib-path ~/src/weisssrv-lib
    python3 tests/validate_render.py --skip ansible

--lib-path points at a weisssrv-lib checkout (the directory holding
`ansible_collections/`), which is how an unreleased collection is exercised;
without it the collection is installed from the pinned ref in the render's
requirements.yml, which needs network access to the git host.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_cluster

CRD_CATALOG = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Failure(Exception):
    pass


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise Failure(f"{tool} is not on PATH (install it or pass --skip)")
    return path


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


# --------------------------------------------------------------------------


def check_yamllint(render: Path) -> None:
    _need("yamllint")
    targets = [d for d in ("ansible", "kubernetes", "terraform", "scripts") if (render / d).is_dir()]
    targets += [f for f in (".gitlab-ci.yml", "Taskfile.yml") if (render / f).is_file()]
    if not targets:
        raise Failure("nothing to lint — the render has no ansible/, kubernetes/ or terraform/")
    # The render's own config, so this matches its CI job and `task lint`.
    config = ["-c", ".yamllint"] if (render / ".yamllint").is_file() else ["-d", "relaxed"]
    result = _run(["yamllint", *config, *targets], cwd=render)
    if result.returncode:
        raise Failure("yamllint:\n" + result.stdout + result.stderr)
    print(f"  yamllint ok ({' '.join(targets)})")


def _flux_lint_inputs(render: Path) -> dict:
    """The flux-lint include's inputs, so this and CI can never disagree about
    which render helper and which ConfigMaps are in play."""
    ci = render_cluster.load_ci(render / ".gitlab-ci.yml")
    for inc in ci.get("include", []):
        if isinstance(inc, dict) and inc.get("file") == "/ci/validate/flux-lint.yml":
            return inc.get("inputs") or {}
    raise Failure("the generated pipeline has no flux-lint include")


def _configmap_paths(render: Path) -> list[str]:
    return str(_flux_lint_inputs(render)["versions_configmap"]).split()


def _render_script(render: Path) -> str:
    return str(_flux_lint_inputs(render).get("flux_render_script", "scripts/flux-render.sh"))


def _substitutions(render: Path, configmaps: list[str]) -> dict[str, str]:
    """Run the repo's own render helper, so the CI entry point is exercised too."""
    script = _render_script(render)
    result = _run(["bash", script, "export-versions", " ".join(configmaps)], cwd=render)
    if result.returncode:
        raise Failure(f"{script} export-versions:\n" + result.stderr)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("export "):
            continue
        key, _, raw = line[len("export ") :].partition("=")
        if key == "FLUX_ENVSUBST_VARS":
            continue
        values[key] = shlex.split(raw)[0] if raw else ""
    if not values:
        raise Failure(f"{script} exported no substitution keys")
    return values


def check_flux(render: Path) -> None:
    _need("kustomize")
    _need("kubeconform")
    configmaps = _configmap_paths(render)
    values = _substitutions(render, configmaps)

    ver = _run(
        ["bash", _render_script(render), "k8s-version", " ".join(configmaps)], cwd=render
    )
    k8s_version = ver.stdout.strip() or "1.36.0"

    cluster_dirs = sorted((render / "kubernetes" / "clusters").glob("*"))
    if not cluster_dirs:
        raise Failure("no kubernetes/clusters/<name>/ in the render")

    failures: list[str] = []
    built = 0
    for cluster_dir in cluster_dirs:
        for ks_file in sorted(cluster_dir.glob("*.yaml")):
            if ks_file.stem == "kustomization":
                continue
            doc = yaml.safe_load(ks_file.read_text()) or {}
            src = (doc.get("spec") or {}).get("path", "")
            if not src:
                continue
            src = src.lstrip("./")
            build = _run(["kustomize", "build", src], cwd=render)
            if build.returncode:
                failures.append(f"kustomize build {src}:\n{build.stderr}")
                continue
            unknown = sorted({m for m in PLACEHOLDER.findall(build.stdout) if m not in values})
            if unknown:
                failures.append(
                    f"{src}: placeholders with no ConfigMap key (they render EMPTY): {unknown}"
                )
                continue
            rendered = PLACEHOLDER.sub(lambda m: values[m.group(1)], build.stdout)
            conform = _run(
                [
                    "kubeconform",
                    "-strict",
                    "-ignore-missing-schemas",
                    "-kubernetes-version",
                    k8s_version,
                    "-schema-location",
                    "default",
                    "-schema-location",
                    CRD_CATALOG,
                    "-summary",
                    "-",
                ],
                input=rendered,
                cwd=render,
            )
            if conform.returncode:
                failures.append(f"kubeconform {src}:\n{conform.stdout}{conform.stderr}")
            built += 1
    if failures:
        raise Failure("\n".join(failures))
    print(f"  flux ok ({built} Kustomizations, k8s {k8s_version}, {len(values)} substitutions)")


_SITE_DATA_SUFFIXES = {".yml", ".yaml", ".env", ".conf", ".toml"}


def _compare_vendored(
    scripts: Path, lib_scripts: Path, local: set[str], site_data: set[str]
) -> tuple[list[str], list[str]]:
    """(vendored, problems) for one scripts/ directory against the library's.

    `local` names files written here that have no library twin; `site_data`
    names files that are configuration for a vendored tool rather than a tool.
    Anything else with no upstream is reported — a copy the library stopped
    shipping is exactly as invisible as one that drifted.
    """
    vendored, drifted, orphaned = [], [], []
    for path in sorted(scripts.iterdir()):
        if not path.is_file() or path.name in local or path.name == "README.md":
            continue
        if path.suffix in _SITE_DATA_SUFFIXES or path.name in site_data:
            continue  # site data, not a vendored tool
        upstream = lib_scripts / path.name
        if not upstream.is_file():
            orphaned.append(path.name)
            continue
        vendored.append(path.name)
        if upstream.read_bytes() != path.read_bytes():
            drifted.append(path.name)

    problems = []
    if drifted:
        problems.append(
            f"{scripts}: vendored scripts differ from the library at the pinned "
            "ref (re-copy them and review the diff): " + ", ".join(drifted)
        )
    if orphaned:
        problems.append(
            f"{scripts}: holds files the library does not ship, and they are not "
            "declared local: " + ", ".join(orphaned)
        )
    return vendored, problems


def check_vendored(render: Path, lib_path: Path | None) -> None:
    """Every vendored script must be byte-identical to the library's copy.

    TWO trees are checked against the one library checkout:

    * the RENDER's `scripts/` — the template ships copies of weisssrv-lib's
      generic tooling so a generated cluster's CI never has to clone another
      repository. Nothing else notices when a copy drifts: the fix the library
      shipped is simply absent, and the next `task lib:sync` refresh silently
      reverts whatever was edited here. `flux-env.sh` is the one script written
      locally and has no library twin, so it is not compared — it must not be a
      fork of a vendored file either, which is why `flux-render.sh` is compared
      like the rest.
    * this REPOSITORY's own `scripts/` — the template's pipeline vendors the
      release script the same way, and it is the one file here whose drift would
      mis-cut the tag every generated cluster's `copier update` resolves to.

    Both comparisons use the checkout `--lib-path` points at, which CI clones at
    the fixture's `lib_ref`. `_assert_one_lib_ref` below reports — instead of
    comparing — when the template's own pipeline pins something else, so the
    repository's copies are never silently gated against a ref they were not
    taken from.
    """
    if not lib_path:
        raise Failure("--lib-path is required to check the vendored copies")
    lib_scripts = lib_path / "scripts"
    if not lib_scripts.is_dir():
        raise Failure(f"{lib_scripts} does not exist (is --lib-path a weisssrv-lib checkout?)")

    vendored, problems = _compare_vendored(
        render / "scripts",
        lib_scripts,
        local={"flux-env.sh"},
        site_data={"version-registry.py"},
    )
    own_scripts = render_cluster.REPO_ROOT / "scripts"
    own: list[str] = []
    if own_scripts.is_dir():
        # A ref mismatch makes the comparison meaningless, so it REPLACES it —
        # reporting drift against a ref the copies never came from would send
        # whoever reads this to re-copy the wrong file.
        mismatch = _assert_one_lib_ref()
        if mismatch:
            problems += mismatch
        else:
            own, own_problems = _compare_vendored(
                own_scripts, lib_scripts, local=set(), site_data=set()
            )
            problems += own_problems
    if problems:
        raise Failure("\n".join(problems))
    print(
        f"  vendored ok ({len(vendored)} render + {len(own)} template-repo scripts "
        "byte-identical to the library)"
    )


def _assert_one_lib_ref() -> list[str]:
    """The template's own pipeline must pin the ref the fixtures do.

    The render-validate job clones ONE library, at the `lib_ref` in
    answers-weisssrv-shaped.yml, and gates both trees with it. If this
    repository's own includes moved to a different tag, the byte-comparison
    above would still pass or fail — against the wrong ref — so the mismatch is
    reported rather than assumed away.
    """
    root = render_cluster.REPO_ROOT
    fixture = yaml.safe_load((root / "tests" / "answers-weisssrv-shaped.yml").read_text())
    expected = fixture["lib_ref"]
    ci = render_cluster.load_ci(root / ".gitlab-ci.yml")
    refs = {
        inc["ref"]
        for inc in ci.get("include", [])
        if isinstance(inc, dict) and "ref" in inc
    }
    if refs - {expected}:
        return [
            "this template's own .gitlab-ci.yml pins library ref(s) "
            f"{sorted(refs)} but tests/answers-weisssrv-shaped.yml answers "
            f"lib_ref: {expected} — render-validate clones only the latter, so "
            "the vendored-script comparison below would run against a ref the "
            "repository's own copies were never taken from"
        ]
    return []


_ASSIGNMENT = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*:", re.MULTILINE)


def _opt_in_roles(roles_dir: Path) -> set[str]:
    """Library roles that ship `<role>_enabled: false` in defaults/main.yml.

    That default IS the opt-in contract: every task in such a role is gated on
    the flag, so invoking the role without setting the flag runs a play that
    does exactly nothing — successfully.
    """
    found = set()
    for role in sorted(p for p in roles_dir.iterdir() if p.is_dir()):
        defaults = role / "defaults" / "main.yml"
        if not defaults.is_file():
            continue
        doc = yaml.safe_load(defaults.read_text()) or {}
        flag = f"{role.name}_enabled"
        if flag in doc and not doc[flag]:
            found.add(role.name)
    return found


def _role_invocations(playbooks_dir: Path):
    """(playbook, role-name, when-clause-text) for every library role a shipped
    playbook invokes, from `roles:` entries and include_role/import_role tasks."""
    def when_text(entry: dict) -> str:
        clause = entry.get("when")
        return " ".join(clause) if isinstance(clause, list) else str(clause or "")

    for path in sorted(playbooks_dir.rglob("*.yml")):
        try:
            plays = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
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
                    for verb in ("include_role", "import_role"):
                        if isinstance(task.get(verb), dict):
                            yield path, str(task[verb].get("name", "")), when_text(task)


def check_role_opt_ins(render: Path, lib_path: Path | None) -> None:
    """An opt-in role invoked UNCONDITIONALLY must have its flag set in the
    inventory.

    This is the shape of a defect that cannot fail loudly on its own: the
    playbook runs, every task in the role skips on `when: <flag> | bool`, the
    play reports ok, and the thing the role exists to do never happens. It
    shipped exactly once — `acme_certs` was invoked by site.yml while
    `acme_certs_enabled` was set nowhere in the inventory, so no certificate was
    ever issued and the NAS play then died on the missing wildcard cert, several
    phases downstream of the actual cause.

    An invocation GUARDED by the flag itself is fine: that is a deliberate
    "off unless the inventory turns it on", and the guard makes it visible.
    """
    if not lib_path:
        raise Failure("--lib-path is required to read the roles' opt-in defaults")
    roles_dir = lib_path / "ansible_collections" / "weisssrv" / "infra" / "roles"
    if not roles_dir.is_dir():
        raise Failure(f"{roles_dir} does not exist (is --lib-path a weisssrv-lib checkout?)")
    opt_in = _opt_in_roles(roles_dir)
    if not opt_in:
        raise Failure(
            "no role in the collection declares `<role>_enabled: false` — the "
            "opt-in convention this check reads has changed, and it is now "
            "examining nothing"
        )

    inventory = render / "ansible" / "inventories" / "prod"
    assigned: set[str] = set()
    for path in sorted(inventory.rglob("*.yml")) + sorted(inventory.rglob("*.yaml")):
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            assigned.update(_ASSIGNMENT.findall(line))

    checked, problems = 0, []
    for path, role, when in _role_invocations(render / "ansible" / "playbooks"):
        name = role.rsplit(".", 1)[-1]
        if name not in opt_in:
            continue
        checked += 1
        flag = f"{name}_enabled"
        if flag in assigned or flag in when:
            continue
        problems.append(
            f"{path.relative_to(render)} invokes {role} unconditionally, but "
            f"{flag} is set nowhere in inventories/prod — every task in the role "
            "will skip and the play will still report success"
        )
    if not checked:
        raise Failure(
            "no opt-in role invocation was examined — either the playbooks stopped "
            f"using the collection's opt-in roles ({', '.join(sorted(opt_in))}) or "
            "the invocation scan is stale"
        )
    if problems:
        raise Failure("\n".join(problems))
    print(f"  role opt-ins ok ({checked} invocations of {len(opt_in)} opt-in roles)")


# `<var> | default('') | length > 0` / `<var> | default([]) | length > 0` — the
# shape every weisssrv.infra role uses to say "this input is required".
_ASSERTED_NONEMPTY = re.compile(
    r"\b([a-z_][a-z0-9_]*)\s*\|\s*default\(\s*(?:''|\"\"|\[\]|\{\})\s*\)\s*\|\s*length\s*>\s*0"
)
_FALSEY_STRINGS = {"", "false", "no", "off", "0", "none"}


def _ansible_bool(value) -> bool:
    """Ansible's `| bool`, which plain Jinja does not have. Feature flags in the
    collection are written `<flag> | bool`, so the gate below cannot read a
    role's own gating without it."""
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY_STRINGS
    return bool(value)


def _walk_tasks(tasks, inherited: tuple[str, ...] = ()):
    """(task, accumulated when-conditions) for every task, descending into
    block/rescue/always so a `when:` on the enclosing block is not lost — which
    is where every optional feature in this collection is actually gated."""
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        clause = task.get("when")
        conditions = inherited + tuple(
            str(c) for c in (clause if isinstance(clause, list) else [clause] if clause else [])
        )
        nested = False
        for key in ("block", "rescue", "always"):
            if key in task:
                nested = True
                yield from _walk_tasks(task[key], conditions)
        if not nested:
            yield task, conditions


def _reachable_by_default(conditions: tuple[str, ...], defaults: dict) -> bool | None:
    """Would this task run on a host that sets none of the role's own inputs?

    Returns None when the expression uses something this evaluator does not
    model, in which case the caller treats the input as NOT required — a missed
    input is a gap, a false positive is a broken build for every operator.
    """
    import jinja2

    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)  # noqa: S701
    env.filters["bool"] = _ansible_bool
    for condition in conditions:
        try:
            verdict = env.from_string(
                "{% if " + condition + " %}yes{% else %}no{% endif %}"
            ).render(**defaults)
        except Exception:  # noqa: BLE001 - an unmodelled expression, not a failure
            return None
        if verdict != "yes":
            return False
    return True


def _asserted_inputs(role: Path, defaults: dict) -> set[str]:
    """Role-prefixed variables the role asserts non-empty on its DEFAULT path.

    Restricted to `<role_name>_*` because that is the collection's naming
    convention for a role's own inputs; the inventory-wide aliases a role also
    reads are set once in group_vars/all.yml and are covered by their own
    prefixed names. Restricted to `assert` tasks (not `when:`/`loop:` uses of
    the same expression), and to asserts whose accumulated `when:` is true with
    nothing set — an opt-in feature's assert is a contract, not a requirement.
    """
    found: set[str] = set()
    tasks_dir = role / "tasks"
    for path in sorted(tasks_dir.rglob("*.yml")) if tasks_dir.is_dir() else []:
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for task, conditions in _walk_tasks(doc):
            spec = task.get("ansible.builtin.assert") or task.get("assert")
            if not isinstance(spec, dict):
                continue
            if _reachable_by_default(conditions, defaults) is not True:
                continue
            that = spec.get("that")
            for clause in that if isinstance(that, list) else [that] if that else []:
                for name in _ASSERTED_NONEMPTY.findall(str(clause)):
                    if name.startswith(role.name + "_"):
                        found.add(name)
    return found


def _role_defaults(role: Path) -> dict:
    defaults = role / "defaults" / "main.yml"
    doc = yaml.safe_load(defaults.read_text()) if defaults.is_file() else {}
    return doc if isinstance(doc, dict) else {}


def _has_usable_default(defaults: dict, var: str) -> bool:
    """False when defaults/main.yml either omits the key or gives it a value
    that is itself empty — both mean the operator must supply it."""
    if var not in defaults:
        return False
    return defaults[var] not in (None, "", [], {})


def check_required_role_inputs(render: Path, lib_path: Path | None) -> None:
    """A role input the role ASSERTS and gives no default must be set in the
    inventory.

    The sibling opt-in check reads `<role>_enabled: false` defaults, so it sees
    only inputs that HAVE a default. The other half of the same class is an
    input with none: the role asserts it up front, the assert is the first task
    of the first play, and nothing in the template supplies it. That shipped —
    `proxmox_lxc_gateway` / `proxmox_vm_cloudinit_gateway` were asserted by both
    guest-provisioning roles, answered by no copier question and set in no
    group_var, so `task infra:deploy` (SETUP's stated entry point) stopped on
    its very first task in every generated cluster while 74 tests, both renders
    and `task lint` stayed green.

    Static on purpose: it reads the library's own `defaults/main.yml` and
    `assert` tasks rather than replaying a play, so it needs no hosts and costs
    nothing.
    """
    if not lib_path:
        raise Failure("--lib-path is required to read the roles' required inputs")
    roles_dir = lib_path / "ansible_collections" / "weisssrv" / "infra" / "roles"
    if not roles_dir.is_dir():
        raise Failure(f"{roles_dir} does not exist (is --lib-path a weisssrv-lib checkout?)")

    inventory = render / "ansible" / "inventories" / "prod"
    assigned: set[str] = set()
    for path in sorted(inventory.rglob("*.yml")) + sorted(inventory.rglob("*.yaml")):
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            assigned.update(_ASSIGNMENT.findall(line))

    # Group vars as VALUES, not just names: a role's feature flag defaults to
    # true in the library and false in this template (restic_offsite is the
    # live example), so an assert's `when:` can only be judged with the
    # inventory's answer in hand.
    group_values: dict = {}
    for path in sorted((inventory / "group_vars").glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        if isinstance(doc, dict):
            group_values.update(doc)

    invoked = {
        role.rsplit(".", 1)[-1]: path
        for path, role, _when in _role_invocations(render / "ansible" / "playbooks")
        if role
    }
    required, problems = 0, []
    for name, playbook in sorted(invoked.items()):
        role = roles_dir / name
        if not role.is_dir():
            continue
        defaults = _role_defaults(role)
        for var in sorted(_asserted_inputs(role, {**defaults, **group_values})):
            if _has_usable_default(defaults, var):
                continue
            required += 1
            if var in assigned:
                continue
            problems.append(
                f"{playbook.relative_to(render)} invokes weisssrv.infra.{name}, which "
                f"asserts {var} and gives it no default in defaults/main.yml — it is "
                "set nowhere in inventories/prod, so the role's opening assert fails "
                "on every host it touches"
            )
    if not required:
        raise Failure(
            "no invoked role declares an assert-without-default input — either the "
            "collection dropped the convention or the assert scan is stale; this "
            "check is now examining nothing"
        )
    if problems:
        raise Failure("\n".join(sorted(set(problems))))
    print(f"  required role inputs ok ({required} asserted-without-default inputs assigned)")


def check_ansible(render: Path, lib_path: Path | None, workdir: Path) -> None:
    _need("ansible-playbook")
    ansible_dir = render / "ansible"
    playbooks = sorted((ansible_dir / "playbooks").glob("*.yml")) if ansible_dir.is_dir() else []
    if not playbooks:
        raise Failure("the render ships no ansible/playbooks/*.yml")

    env = dict(os.environ)
    env.pop("ANSIBLE_COLLECTIONS_PATHS", None)  # ansible-compat hard-errors on the plural spelling
    dest = workdir / "collections"
    requirements = ansible_dir / "requirements.yml"
    if lib_path:
        # The checkout supplies weisssrv.infra; its galaxy DEPENDENCIES
        # (ansible.posix, community.general) still have to come from somewhere,
        # or every FQCN module reference in the roles fails to resolve.
        filtered = workdir / "requirements-deps.yml"
        doc = yaml.safe_load(requirements.read_text()) or {}
        doc["collections"] = [
            entry
            for entry in doc.get("collections") or []
            if not str(entry.get("name", "")).startswith("git+")
        ]
        filtered.write_text(yaml.safe_dump(doc))
        _run(
            ["ansible-galaxy", "collection", "install", "-r", str(filtered), "-p", str(dest)],
            cwd=ansible_dir,
            env=env,
        )
        search = [str(lib_path), str(dest)]
    else:
        install = _run(
            ["ansible-galaxy", "collection", "install", "-r", "requirements.yml", "-p", str(dest)],
            cwd=ansible_dir,
            env=env,
        )
        if install.returncode:
            raise Failure("ansible-galaxy collection install:\n" + install.stdout + install.stderr)
        search = [str(dest)]
    # The operator's own collections are the offline fallback when the galaxy
    # install above could not reach the network.
    search.append(str(Path.home() / ".ansible" / "collections"))
    env["ANSIBLE_COLLECTIONS_PATH"] = ":".join(search)

    failures = []
    for playbook in playbooks:
        result = _run(
            [
                "ansible-playbook",
                "--syntax-check",
                "-i",
                "inventories/prod",
                str(playbook.relative_to(ansible_dir)),
            ],
            cwd=ansible_dir,
            env=env,
        )
        if result.returncode:
            failures.append(f"{playbook.name}:\n{result.stdout}{result.stderr}")
    if failures:
        raise Failure("\n".join(failures))
    print(f"  ansible ok ({len(playbooks)} playbooks syntax-checked)")


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path, help="Validate this render instead of a new one.")
    parser.add_argument("--answers", type=Path, default=render_cluster.ANSWERS)
    parser.add_argument("--lib-path", type=Path, help="weisssrv-lib checkout for the collection.")
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated: yamllint,flux,vendored,role-opt-ins,role-inputs,ansible",
    )
    args = parser.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    workdir = Path(tempfile.mkdtemp(prefix="validate-render-"))
    try:
        render = args.render_dir or render_cluster.render(workdir, answers=args.answers)
        print(f"validating {render}")
        checks = (("yamllint", check_yamllint), ("flux", check_flux))
        failed = []
        for name, fn in checks:
            if name in skip:
                print(f"  {name} skipped")
                continue
            try:
                fn(render)
            except Failure as exc:
                failed.append(f"[{name}] {exc}")
        for name, fn in (
            ("vendored", check_vendored),
            ("role-opt-ins", check_role_opt_ins),
            ("role-inputs", check_required_role_inputs),
        ):
            if name in skip:
                print(f"  {name} skipped")
            elif not args.lib_path:
                print(f"  {name} skipped (no --lib-path)")
            else:
                try:
                    fn(render, args.lib_path)
                except Failure as exc:
                    failed.append(f"[{name}] {exc}")
        if "ansible" in skip:
            print("  ansible skipped")
        else:
            try:
                check_ansible(render, args.lib_path, workdir)
            except Failure as exc:
                failed.append(f"[ansible] {exc}")
        if failed:
            print("\nFAILED:\n" + "\n\n".join(failed), file=sys.stderr)
            return 1
        print("render validated")
        return 0
    finally:
        if not args.render_dir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
