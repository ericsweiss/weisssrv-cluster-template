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
import ast
import difflib
import ipaddress
import json
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


def _strip_hujson(text: str) -> str:
    """HuJSON -> JSON: drop `//` comments and trailing commas.

    String-aware, so a `//` inside a quoted value survives.
    """
    out, in_string, escaped, i = [], False, False, 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def check_terraform(render: Path) -> None:
    """Canonical formatting, and the tailnet policy still parses.

    `terraform fmt -check` is the cheapest gate that catches a Jinja bug landing
    as invalid HCL: five of the Terraform files are templates. It needs no
    network and no credentials.
    """
    tf = render / "terraform"
    if not tf.is_dir():
        raise Failure("the render has no terraform/")
    _need("terraform")
    result = _run(["terraform", "fmt", "-check", "-recursive", "terraform/"], cwd=render)
    if result.returncode:
        raise Failure(
            "terraform fmt -check reported unformatted files (the rendered HCL "
            "is not canonical, or a template emitted broken syntax):\n"
            + result.stdout
            + result.stderr
        )
    policies = sorted(tf.glob("*/policy.hujson"))
    for policy in policies:
        try:
            json.loads(_strip_hujson(policy.read_text()))
        except json.JSONDecodeError as exc:
            raise Failure(f"{policy.relative_to(render)} is not valid HuJSON: {exc}") from exc
    modules = len(list(tf.glob("*/versions.tf")))
    print(f"  terraform fmt ok ({modules} modules, {len(policies)} policy documents)")


_MODULE_SOURCE = re.compile(r'"git::[^"]*//terraform/modules/([A-Za-z0-9_-]+)\?ref=[^"]*"')


def check_terraform_validate(render: Path, lib_path: Path | None) -> None:
    """`terraform validate` per module, against the library checkout.

    The shipped sources are `git::…?ref=<tag>`, which would validate the
    RELEASED module rather than the one under review — so each source is
    rewritten to the local checkout in a throwaway copy. `-backend=false` skips
    state and credentials; the provider itself still comes from the registry.

    `-lockfile=readonly` is what CI's plan and apply pass, so a root pin the
    committed `.terraform.lock.hcl` no longer satisfies has to fail here rather
    than at deploy time.
    """
    _need("terraform")
    modules = sorted(p.parent for p in (render / "terraform").glob("*/versions.tf"))
    if not modules:
        raise Failure("no Terraform modules in the render")
    work = Path(tempfile.mkdtemp(prefix="tf-validate-"))
    problems = []
    try:
        for module in modules:
            target = work / module.name
            shutil.copytree(module, target)
            for tf_file in target.glob("*.tf"):
                text = tf_file.read_text()
                rewritten = _MODULE_SOURCE.sub(
                    lambda m: f'"{lib_path}/terraform/modules/{m.group(1)}"', text
                )
                if rewritten != text:
                    tf_file.write_text(rewritten)
            init = _run(
                ["terraform", "init", "-backend=false", "-input=false", "-lockfile=readonly"],
                cwd=target,
            )
            if init.returncode:
                problems.append(f"{module.name}: init failed\n{init.stdout}{init.stderr}")
                continue
            validate = _run(["terraform", "validate"], cwd=target)
            if validate.returncode:
                problems.append(f"{module.name}:\n{validate.stdout}{validate.stderr}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if problems:
        raise Failure("terraform validate:\n" + "\n\n".join(problems))
    print(f"  terraform validate ok ({', '.join(m.name for m in modules)})")


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


# The invariant gates the generated pipeline runs over its own manifests. Each
# reads the WHOLE rendered corpus on stdin; `netpol-parity` is separate because
# it reads the manifests on disk (it also covers kubernetes/clusters/*/, which
# no Kustomization builds).
_CORPUS_GATES = (
    ("check-scrape-netpol.py", ()),
    ("check-default-deny-coverage.py", ()),
    ("check-secretstore-scope.py", ()),
    ("check-pvc-storageclass.py", ()),
    (
        "check-hpa-vpa-invariant.py",
        ("--require-chart-native-vpas", "--policy-config", "scripts/autoscaling-policy.yaml"),
    ),
)


def check_cluster_gates(render: Path) -> None:
    """Run the shipped invariant gates over the shipped manifests.

    The render suite asserts the pipeline WIRES these; this asserts the payload
    they are wired over actually passes them. Without it a generated cluster's
    very first pipeline can be red on manifests nobody edited, and the template
    change that caused it went green.
    """
    _need("kustomize")
    configmaps = _configmap_paths(render)
    values = _substitutions(render, configmaps)

    corpus: list[str] = []
    for cluster_dir in sorted((render / "kubernetes" / "clusters").glob("*")):
        for ks_file in sorted(cluster_dir.glob("*.yaml")):
            if ks_file.stem == "kustomization":
                continue
            src = ((yaml.safe_load(ks_file.read_text()) or {}).get("spec") or {}).get("path", "")
            if not src:
                continue
            build = _run(["kustomize", "build", src.lstrip("./")], cwd=render)
            if build.returncode:
                raise Failure(f"kustomize build {src}:\n{build.stderr}")
            corpus.append(PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), build.stdout))
    if not corpus:
        raise Failure("no Kustomization rendered — the gates would examine nothing")
    stream = "\n---\n".join(corpus)

    failures = []
    for script, extra in _CORPUS_GATES:
        if not (render / "scripts" / script).is_file():
            failures.append(f"{script} is wired into CI but not shipped")
            continue
        result = _run([sys.executable, f"scripts/{script}", *extra], input=stream, cwd=render)
        if result.returncode:
            failures.append(f"{script}:\n{result.stdout}{result.stderr}")

    netpol = _run(
        [
            sys.executable,
            "scripts/check-netpol-except-parity.py",
            "--config",
            "scripts/netpol-except.yaml",
            "kubernetes/",
        ],
        cwd=render,
    )
    if netpol.returncode:
        failures.append(f"check-netpol-except-parity.py:\n{netpol.stdout}{netpol.stderr}")

    if failures:
        raise Failure("\n".join(failures))
    print(f"  cluster gates ok ({len(_CORPUS_GATES) + 1} invariants over {len(corpus)} builds)")


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
      reverts whatever was edited here. Every file in the render's `scripts/` —
      `flux-env.sh`, `flux-render.sh` and `check-default-deny-coverage.py`
      included — is compared, and an unlisted file with no twin is reported
      rather than skipped.
    * this REPOSITORY's own `scripts/` — the template's pipeline vendors the
      release script the same way, and it is the one file here whose drift would
      mis-cut the tag every generated cluster's `copier update` resolves to.

    Plus this repository's own vendored manifest (`_check_registered_copies`).
    It reads `template/scripts/` rather than the render, so the overlap with the
    first comparison is deliberate — together they prove the copy is right
    BEFORE copier touches it and unchanged AFTER. What only the manifest can see
    is the rest: the shared test suite under `tests/`, the secret-detection
    ruleset under `.gitlab/`, and the lint profiles this repository deliberately
    forks, where the silent failure runs the other way
    (the library moves and the fork never absorbs it).

    Both comparisons use the checkout `--lib-path` points at, which CI clones at
    copier.yml's `lib_ref` default (the single source the fixtures inherit).
    `_assert_one_lib_ref` below reports — instead of
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
        local=set(),
        site_data={"version-registry.py"},
    )
    problems += _check_registered_copies(lib_path)
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


MANIFEST_RELPATH = "scripts/vendored-manifest.yml"


def _check_registered_copies(lib_path: Path) -> list[str]:
    """Run the library's vendored-copy engine over this repository's manifest.

    The scripts/ comparison above walks DIRECTORIES, so it only ever sees copies
    that live in a scripts/ tree. This repository's own manifest
    (scripts/vendored-manifest.yml) is the authority on every copy relationship,
    including the ones no directory walk reaches: the canonical check-lib-pins
    test suite under tests/, the secret-detection ruleset under .gitlab/, and the
    lint profiles this repository deliberately FORKS — where the failure is
    silent in the other direction (the library moves and the fork never absorbs
    it).

    The list lives HERE rather than in the library: the library publishes only an
    offer list (scripts/vendorable-paths.yml) bounding what a manifest may name,
    and the engine that reads both. Moving a copy inside this repository is
    therefore an edit to the manifest in the same commit, not a library release.
    """
    checker = lib_path / "scripts" / "check-vendored-copies.py"
    manifest = render_cluster.REPO_ROOT / MANIFEST_RELPATH
    if not checker.is_file():
        return [
            f"{lib_path} ships no scripts/check-vendored-copies.py — the vendored-copy "
            "gate cannot run, and it must not silently skip"
        ]
    if not manifest.is_file():
        return [
            f"{manifest} is missing — this repository's vendored copies would go "
            "ungated, and the gate must not silently skip"
        ]
    result = _run(
        [
            sys.executable,
            str(checker),
            "--manifest",
            str(manifest),
            "--repo-root",
            str(render_cluster.REPO_ROOT),
            "--lib-path",
            str(lib_path),
        ]
    )
    if result.returncode:
        return [f"vendored copies ({MANIFEST_RELPATH}):\n{result.stdout}{result.stderr}"]
    return []


def _assert_one_lib_ref() -> list[str]:
    """The template's own pipeline must pin the ref the fixtures resolve to.

    The render-validate job clones ONE library, at copier.yml's `lib_ref` default
    (the single source the fixtures inherit), and gates both trees with it. If
    this repository's own includes moved to a different tag, the byte-comparison
    above would still pass or fail — against the wrong ref — so the mismatch is
    reported rather than assumed away.
    """
    root = render_cluster.REPO_ROOT
    # The fixtures inherit lib_ref from copier.yml's default (the single source),
    # so that default is the ref render-validate clones — and the one this
    # template's own includes (if any) must match.
    expected = yaml.safe_load((root / "copier.yml").read_text())["lib_ref"]["default"]
    ci = render_cluster.load_ci(root / ".gitlab-ci.yml")
    refs = {
        inc["ref"]
        for inc in ci.get("include", [])
        if isinstance(inc, dict) and "ref" in inc
    }
    if refs - {expected}:
        return [
            "this template's own .gitlab-ci.yml pins library ref(s) "
            f"{sorted(refs)} but copier.yml's lib_ref default is {expected} — "
            "render-validate clones only the latter, so "
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


_UNMODELLED = object()


def _render_default(value, context: dict):
    """The value a role's default actually takes on an inventory holding
    `context`.

    A default is routinely an EXPRESSION over the inventory rather than a
    literal, and Ansible converts a whole-template result back to a native type
    — `proxmox_vm_cloudinit_dns: "{{ dns_servers | default([]) }}"` is an empty
    LIST on an inventory that sets no `dns_servers`, not the two-character
    string `"[]"` a plain Jinja render returns. Testing the raw string instead
    reads every such default as "supplied".

    Returns `_UNMODELLED` when the expression uses something this evaluator does
    not model, which the caller reads as "assume it has a default": a missed
    input is a gap, a false positive is a broken build for every operator.
    """
    if not isinstance(value, str) or "{{" not in value:
        return value
    import jinja2

    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)  # noqa: S701
    env.filters["bool"] = _ansible_bool
    try:
        rendered = env.from_string(value).render(**context)
    except Exception:  # noqa: BLE001 - an unmodelled expression, not a failure
        return _UNMODELLED
    try:
        return ast.literal_eval(rendered)
    except (ValueError, SyntaxError):
        return rendered


def _referenced_names(value) -> set[str]:
    """Inventory variables a default's expression reads."""
    if not isinstance(value, str) or "{{" not in value:
        return set()
    import jinja2
    from jinja2 import meta

    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)  # noqa: S701
    try:
        return meta.find_undeclared_variables(env.parse(value))
    except Exception:  # noqa: BLE001 - an unmodelled expression, not a failure
        return set()


def _is_empty(value) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value in (None, [], {}, ())


def _default_gap(defaults: dict, var: str, context: dict, assigned: set[str]) -> str | None:
    """None when defaults/main.yml gives `var` a value that is non-empty ONCE
    RENDERED against this inventory; otherwise the reason it does not.

    Two gaps, and the second is the one a raw-string test cannot see: the key is
    absent, or the key is present and its value renders EMPTY (an expression
    over a name the inventory does not set is non-empty as TEXT and empty as a
    VALUE).

    An empty render whose expression reads a name that IS set somewhere in the
    inventory but outside `context` is treated as supplied, so the check cannot
    invent work for a value that simply lives where this evaluator does not look.
    """
    if var not in defaults:
        return "gives it no default in defaults/main.yml"
    raw = defaults[var]
    value = _render_default(raw, context)
    if value is _UNMODELLED or not _is_empty(value):
        return None
    if (_referenced_names(raw) & assigned) - set(context):
        return None
    if isinstance(raw, str) and "{{" in raw:
        return f"defaults it to {raw!r}, which renders EMPTY against this inventory"
    return f"defaults it to {raw!r}, which is empty"


def check_required_role_inputs(render: Path, lib_path: Path | None) -> None:
    """A role input the role ASSERTS and has no usable default for must be set
    in the inventory. The sibling opt-in check covers inputs that HAVE a
    default; this is the other half — the assert is the first task of the first
    play, so a missing input stops `task infra:deploy` immediately.

    "Usable" is decided by RENDERING the default, not by reading it: an input
    defaulting to an expression over `dns_servers` reads as supplied and
    evaluates to nothing when the inventory stops setting it. See `_default_gap`.

    Static on purpose — it reads the library's `defaults/main.yml` and `assert`
    tasks rather than replaying a play. The price is scope: `assigned` is every
    name assigned anywhere under `inventories/prod`, not the subset a given
    group inherits, so it under-reports rather than over-reports.
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
        context = {**defaults, **group_values}
        for var in sorted(_asserted_inputs(role, context)):
            gap = _default_gap(defaults, var, context, assigned)
            if gap is None:
                continue
            required += 1
            if var in assigned:
                continue
            problems.append(
                f"{playbook.relative_to(render)} invokes weisssrv.infra.{name}, which "
                f"asserts {var} and {gap} — and {var} is set nowhere in "
                "inventories/prod, so the role's opening assert fails on every host "
                "it touches"
            )
    if not required:
        raise Failure(
            "no invoked role declares an asserted input without a usable default — "
            "either the collection dropped the convention or the assert scan is "
            "stale; this check is now examining nothing"
        )
    if problems:
        raise Failure("\n".join(sorted(set(problems))))
    print(f"  required role inputs ok ({required} asserted inputs with no usable default assigned)")


def _inventory_hosts(hosts_file: Path) -> dict[str, dict]:
    """host name -> merged vars, for every host in an Ansible YAML inventory.

    Merged rather than collected per group: a host listed in two groups
    (`dns-01` is in both `dns` and `dns_primary`) is ONE machine, and comparing
    its two entries against each other would report every such host as a
    duplicate of itself.
    """
    hosts: dict[str, dict] = {}

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "hosts" and isinstance(value, dict):
                for name, host_vars in value.items():
                    hosts.setdefault(name, {}).update(host_vars or {})
            elif key != "vars":
                walk(value)

    walk(yaml.safe_load(hosts_file.read_text()) or {})
    return hosts


def check_inventory_addresses(render: Path) -> None:
    """No two guests may share an address or a vmid, no guest may hold one of
    the cluster's VIPs, and every address must sit inside the LAN.

    The copier answers are validated one at a time, never against the plan they
    compose into: `hosts.yml` hardcodes every address and vmid except the
    resolvers', whose vmid is DERIVED from the answer (`100 + last octet`), so a
    resolver answered into another guest's band collides with it while every
    static gate stays green. `pct` and `qm` share one vmid namespace, so the
    collision surfaces two phases later as a create against a known id.

    Deliberately a check on the RENDERED INVENTORY rather than on the answer:
    `hosts.yml` is a skeleton the operator re-addresses by hand, and a hand edit
    reaches the same collision by a route no answer validator sees. The
    generated repo carries the same assertions in its own
    `tests/test_cluster_invariants.py`; this copy gates the template itself.
    """
    hosts_file = render / "ansible" / "inventories" / "prod" / "hosts.yml"
    if not hosts_file.is_file():
        raise Failure(f"{hosts_file} does not exist — the render has no Ansible inventory")
    hosts = _inventory_hosts(hosts_file)
    if not hosts:
        raise Failure(f"{hosts_file} declares no hosts — this check is examining nothing")

    problems: list[str] = []
    for field in ("ansible_host", "vmid"):
        owners: dict[str, list[str]] = {}
        for name, host_vars in sorted(hosts.items()):
            value = host_vars.get(field)
            if value is not None:
                owners.setdefault(str(value), []).append(name)
        for value, names in sorted(owners.items()):
            if len(names) > 1:
                problems.append(f"{field} {value} is claimed by {', '.join(names)}")

    cidr = _lan_cidr(render)
    if cidr is None:
        problems.append(
            "no cluster_lan_cidr in kubernetes/infrastructure/sources/cluster-config.yaml — "
            "the addresses below cannot be checked against the LAN"
        )
    else:
        network = ipaddress.ip_network(cidr, strict=False)
        for name, host_vars in sorted(hosts.items()):
            addr = host_vars.get("ansible_host")
            if addr is None:
                continue
            try:
                inside = ipaddress.ip_address(str(addr)) in network
            except ValueError:
                continue  # a name rather than an address; DNS resolves it
            if not inside:
                problems.append(f"{name}'s ansible_host {addr} is outside lan_cidr {network}")

    # The floating addresses are not inventory hosts, so the duplicate scan
    # above cannot see them: kube-vip and MetalLB answer ARP for them, and a
    # guest holding the same address is an ARP fight that names neither. The
    # copier validators reject a VIP inside a composed band; this is the hand-
    # edit route to the same collision.
    vips = {
        "cluster_k3s_api_vip": "the k3s API VIP",
        "cluster_metallb_public_vip": "the public MetalLB VIP",
        "cluster_metallb_internal_vip": "the internal MetalLB VIP",
    }
    config = _cluster_config(render)
    claimed = {str(config[key]): label for key, label in vips.items() if config.get(key)}
    missing = sorted(k for k in vips if not config.get(k))
    if missing:
        problems.append(
            "cluster-config declares no " + ", ".join(missing) + " — those VIPs "
            "are compared against nothing"
        )
    for name, host_vars in sorted(hosts.items()):
        addr = str(host_vars.get("ansible_host") or "")
        if addr in claimed:
            problems.append(f"{name}'s ansible_host {addr} is {claimed[addr]}")

    if problems:
        raise Failure(
            f"{hosts_file.relative_to(render)}:\n  "
            + "\n  ".join(problems)
            + "\n  (pct and qm share one vmid namespace, and two guests on one "
            "address fail several phases after the answer that caused it)"
        )
    print(
        f"  inventory addresses ok ({len(hosts)} hosts, unique vmid + address, "
        f"none on a VIP, all inside {cidr})"
    )


def _cluster_config(render: Path) -> dict:
    """The cluster-config ConfigMap's data, the cluster's declared single source
    for its site values — not the answers file, which a re-addressed inventory
    outgrows."""
    config = render / "kubernetes" / "infrastructure" / "sources" / "cluster-config.yaml"
    if not config.is_file():
        return {}
    for doc in yaml.safe_load_all(config.read_text()):
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            return doc.get("data") or {}
    return {}


def _lan_cidr(render: Path) -> str | None:
    value = _cluster_config(render).get("cluster_lan_cidr")
    return str(value) if value else None


def check_version_coverage(render: Path) -> None:
    """Every pin in the rendered vars file has a version-registry entry.

    Offline (it compares two local files) and run here rather than only in the
    generated cluster's own pipeline, because the pins and the registry are BOTH
    template output: an entry added to one .jinja and not the other produces a
    cluster whose weekly bump bot silently never reports that pin.
    """
    checker = render / "scripts" / "check-versions.py"
    if not checker.is_file():
        raise Failure(f"{checker} is missing from the render")
    result = _run([sys.executable, str(checker), "--check-coverage"], cwd=render)
    if result.returncode:
        raise Failure(result.stdout + result.stderr)
    print("  version coverage ok (every pin has a registry entry)")


def check_versions_configmap(render: Path) -> None:
    """The rendered cluster-versions ConfigMap matches the rendered vars file.

    Both are template output from separate .jinja files, so a pin bumped in one
    and not the other renders a cluster that fails its own `task lint:repo-sync`
    on the first run. Nothing else here catches it: check_flux substitutes FROM
    the ConfigMap, so a stale value is a valid render.
    """
    generator = render / "scripts" / "generate-versions-configmap.py"
    shipped = render / "kubernetes" / "infrastructure" / "sources" / "versions-configmap.yaml"
    if not generator.is_file():
        raise Failure(f"{generator} is missing from the render")
    if not shipped.is_file():
        raise Failure(f"{shipped} is missing from the render")
    with tempfile.TemporaryDirectory() as tmp:
        regenerated = Path(tmp) / "versions-configmap.yaml"
        result = _run(
            [
                sys.executable,
                str(generator),
                "--vars-file",
                "ansible/inventories/prod/group_vars/all.yml",
                "--output",
                str(regenerated),
                "--nested-key",
                "helm_chart_versions",
                "--regen-command",
                "task flux:sync-versions",
            ],
            cwd=render,
        )
        if result.returncode:
            raise Failure(result.stdout + result.stderr)
        want = regenerated.read_text()
    have = shipped.read_text()
    if have != want:
        diff = "".join(
            difflib.unified_diff(
                have.splitlines(keepends=True),
                want.splitlines(keepends=True),
                fromfile="rendered versions-configmap.yaml",
                tofile="regenerated from all.yml",
            )
        )
        raise Failure(
            "versions-configmap.yaml.jinja and all.yml.jinja disagree — bump both "
            f"in the same change:\n{diff}"
        )
    print("  versions configmap ok (in sync with the rendered all.yml)")


_PIPELINE_KEYS = {"stages", "workflow", "variables", "include", "default"}


def _image_name(image) -> str | None:
    """The image reference, whether written as a string or a `name:` mapping."""
    if isinstance(image, str):
        return image
    if isinstance(image, dict) and isinstance(image.get("name"), str):
        return image["name"]
    return None


def _unpinned(image) -> str | None:
    """Why this image is not pinned, or None when it is."""
    name = _image_name(image)
    if name is None:
        return f"unreadable image reference {image!r}"
    tag = name.rsplit("/", 1)[-1]
    if ":" not in tag:
        return f"{name} carries no tag, so it resolves to whatever :latest is today"
    if tag.rsplit(":", 1)[-1] == "latest":
        return f"{name} pins :latest, which is not a pin"
    return None


def check_ci_policy(render: Path) -> None:
    """The generated pipeline's image and cancellation policy.

    Both are defaults a job inherits by saying nothing, which is exactly why they
    need a gate: no rendered job FAILS when they are missing.

    * `default.image` — a job with no image runs on whatever the runner names as
      its default. That is unreviewable, and the deploy fragment apt-installs, so
      a non-Debian runner default breaks the deploy on its first line.
    * `default.interruptible: true` plus `workflow.auto_cancel` — without the
      pair, a superseded merge-request pipeline is never cancelled. Without the
      `main` override back to `none`, the opposite failure: a second merge
      cancels the first's jobs, skipping validation-gate and leaving the first
      merge's deploys unmet and never run.
    * anything that touches live infrastructure overrides `interruptible` back to
      false, so it is never the job that gets cancelled.
    """
    ci = render_cluster.load_ci(render / ".gitlab-ci.yml")
    problems: list[str] = []

    default = ci.get("default") or {}
    if not isinstance(default, dict) or "image" not in default:
        problems.append("no top-level `default: image:` — every job's image is the runner's choice")
    else:
        why = _unpinned(default["image"])
        if why:
            problems.append(f"default image: {why}")
    if default.get("interruptible") is not True:
        problems.append("`default: interruptible: true` is missing — nothing is cancellable")

    workflow = ci.get("workflow") or {}
    if (workflow.get("auto_cancel") or {}).get("on_new_commit") != "interruptible":
        problems.append(
            "workflow.auto_cancel.on_new_commit is not `interruptible` — GitLab's "
            "conservative default stops cancelling once any job has started"
        )
    main_rules = [
        rule
        for rule in workflow.get("rules") or []
        if isinstance(rule, dict) and 'CI_COMMIT_BRANCH == "main"' in str(rule.get("if", ""))
    ]
    if not main_rules:
        problems.append("workflow has no `main` rule to carry the auto_cancel override")
    elif not any(
        (rule.get("auto_cancel") or {}).get("on_new_commit") == "none" for rule in main_rules
    ):
        problems.append(
            "the `main` workflow rule does not override auto_cancel to `none` — a "
            "second merge would cancel the first's pipeline mid-deploy"
        )

    jobs = {
        name: body
        for name, body in ci.items()
        if name not in _PIPELINE_KEYS and isinstance(body, dict)
    }
    for name, body in sorted(jobs.items()):
        if "image" in body:
            why = _unpinned(body["image"])
            if why:
                problems.append(f"{name}: {why}")
    live = [
        name
        for name, body in sorted(jobs.items())
        if body.get("stage") in {"deploy", "gate"} or name in {"terraform-plan"}
    ]
    for name in live:
        body = jobs[name]
        extends = body.get("extends")
        extends = [extends] if isinstance(extends, str) else list(extends or [])
        if body.get("interruptible") is False or ".deploy-base" in extends:
            continue
        problems.append(
            f"{name} touches live infrastructure but is interruptible — set "
            "`interruptible: false` or extend .deploy-base"
        )

    if problems:
        raise Failure("\n".join(problems))
    print(
        f"  ci policy ok (pinned default image, auto_cancel split, {len(live)} "
        "uninterruptible live-infrastructure jobs)"
    )


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


# The two check registries, module-level so the --skip help, docs/CI.md and the
# test that holds them equal all read the same list.
RENDER_CHECKS = (
    ("yamllint", check_yamllint),
    ("terraform", check_terraform),
    ("flux", check_flux),
    ("cluster-gates", check_cluster_gates),
    ("ci-policy", check_ci_policy),
    ("inventory-addresses", check_inventory_addresses),
    ("version-coverage", check_version_coverage),
    ("versions-configmap", check_versions_configmap),
)

# These take the library checkout as well, so they skip without --lib-path.
LIB_CHECKS = (
    ("vendored", check_vendored),
    ("role-opt-ins", check_role_opt_ins),
    ("role-inputs", check_required_role_inputs),
    ("terraform-validate", check_terraform_validate),
)

CHECK_NAMES = tuple(name for name, _ in RENDER_CHECKS + LIB_CHECKS) + ("ansible",)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path, help="Validate this render instead of a new one.")
    parser.add_argument("--answers", type=Path, default=render_cluster.ANSWERS)
    parser.add_argument("--lib-path", type=Path, help="weisssrv-lib checkout for the collection.")
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated: " + ",".join(CHECK_NAMES),
    )
    args = parser.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    unknown = skip - set(CHECK_NAMES)
    if unknown:
        # A typo'd name would otherwise skip nothing and report success.
        parser.error(f"--skip names no such check: {', '.join(sorted(unknown))}")
    workdir = Path(tempfile.mkdtemp(prefix="validate-render-"))
    try:
        render = args.render_dir or render_cluster.render(workdir, answers=args.answers)
        print(f"validating {render}")
        failed = []
        for name, fn in RENDER_CHECKS:
            if name in skip:
                print(f"  {name} skipped")
                continue
            try:
                fn(render)
            except Failure as exc:
                failed.append(f"[{name}] {exc}")
        for name, fn in LIB_CHECKS:
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
