"""Validate a rendered cluster with the real toolchain, as its own pipeline does.

`--lib-path` points at a weisssrv-lib checkout at the pinned lib_ref;
`--allow-ref-mismatch` accepts an unreleased one. See docs/CI.md.
"""

from __future__ import annotations

import argparse
import ast
import difflib
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
import md_tables
import render_cluster

CRD_CATALOG = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# apiVersion/Kind pairs kubeconform may validate against NO schema. Empty: the
# catalog covers every kind the template ships, so a skip is either a failed
# catalog fetch or a new CRD arriving unvalidated.
EXPECTED_SKIPPED = frozenset()


class Failure(Exception):
    pass


def _lib_verdict(verified: str, unverified: str, ref_verified: bool) -> str:
    """CRITICAL: a gate that read the library checkout says something about the
    PINNED ref only when that ref resolved. Every lib-reading gate prints
    through this, so none reports a bare `ok` against a checkout at another ref.
    """
    return "  " + (verified if ref_verified else unverified)


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise Failure(f"{tool} is not on PATH (install it or pass --skip)")
    return path


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def _yaml_paths(root: Path, *, recursive: bool = True) -> list[Path]:
    """Every YAML file under `root`, both suffixes.

    A walk that globs one spelling lets a rendered `.yaml` file past the gate
    that was written for `.yml`.
    """
    walk = root.rglob if recursive else root.glob
    return sorted(path for suffix in ("*.yml", "*.yaml") for path in walk(suffix))


# --------------------------------------------------------------------------


YAMLLINT_CONFIG = "lint/yamllint-relaxed.yml"


def check_yamllint(render: Path, **_kw) -> None:
    _need("yamllint")
    dirs = ("ansible", "kubernetes", "terraform", "scripts", "taskfiles", "lint")
    targets = [d for d in dirs if (render / d).is_dir()]
    targets += [f for f in (".gitlab-ci.yml", "Taskfile.yml") if (render / f).is_file()]
    if not targets:
        raise Failure("nothing to lint — the render has no ansible/, kubernetes/ or terraform/")
    # The render's own config, so this matches its CI job and `task lint`.
    # Falling back to `-d relaxed` would lint a different profile than the
    # generated pipeline does and report different findings.
    if not (render / YAMLLINT_CONFIG).is_file():
        raise Failure(
            f"the render ships no {YAMLLINT_CONFIG} — its yaml-lint job and `task lint` "
            "both pass that path, so the gate would check a profile the cluster never uses"
        )
    result = _run(["yamllint", "-c", YAMLLINT_CONFIG, *targets], cwd=render)
    if result.returncode:
        raise Failure("yamllint:\n" + result.stdout + result.stderr)
    print(f"  yamllint ok ({' '.join(targets)})")


def check_shellcheck(render: Path, **_kw) -> None:
    """The rendered shell, at the severity and exclusions the generated pipeline uses.

    `scripts/collect-state.sh` is Jinja in the template, so this render is the
    only place it exists as shell.
    """
    _need("shellcheck")
    targets = sorted(render.glob("scripts/*.sh")) + sorted(render.glob("terraform/*/*.sh"))
    if not targets:
        raise Failure("the render ships no shell under scripts/ or terraform/")
    result = _run(
        [
            "shellcheck",
            "--severity=warning",
            "--exclude=SC1091,SC2034",
            *[str(t.relative_to(render)) for t in targets],
        ],
        cwd=render,
    )
    if result.returncode:
        raise Failure("shellcheck:\n" + result.stdout + result.stderr)
    print(f"  shellcheck ok ({len(targets)} scripts)")


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


# The backend credential and the two lock methods are emitted once, by the
# `tf_backend()` macro; only the state name varies per root.
_TF_SHARED_HTTP_KEYS = (
    "TF_HTTP_USERNAME",
    "TF_HTTP_PASSWORD",
    "TF_HTTP_LOCK_METHOD",
    "TF_HTTP_UNLOCK_METHOD",
)


def tf_state_backend_problems(render: Path) -> list[str]:
    """CRITICAL: a root whose `env:` is inlined, or whose macro call passes
    another root's state name, writes its state over a sibling's. The roots are
    read from the rendered taskfile, so the optional modules shape the set."""
    taskfile = render / "taskfiles" / "terraform.yml"
    if not taskfile.is_file():
        return [f"{taskfile} is missing — the state-backend parity check examined nothing"]
    tasks = (yaml.safe_load(taskfile.read_text()) or {}).get("tasks") or {}
    envs: dict[str, dict] = {}
    for body in tasks.values():
        env = body.get("env") if isinstance(body, dict) else None
        if not isinstance(env, dict) or "TF_HTTP_ADDRESS" not in env:
            continue
        root = str(body.get("dir", "")).rstrip("/").rsplit("/", 1)[-1]
        envs.setdefault(root, env)
    if not envs:
        return ["no terraform task declares TF_HTTP_ADDRESS — the render has no managed state"]

    problems = []
    roots = {path.name for path in (render / "terraform").glob("*") if path.is_dir()}
    if set(envs) != roots:
        problems.append(
            f"terraform roots {sorted(roots)} but state-backed tasks for {sorted(envs)} — "
            "a root without its own state shares a sibling's"
        )
    for key in _TF_SHARED_HTTP_KEYS:
        values = {root: env.get(key) for root, env in envs.items()}
        if len(set(values.values())) != 1:
            problems.append(f"{key} is not identical across the roots: {values}")
    for root, env in envs.items():
        for key, suffix in (
            ("TF_HTTP_ADDRESS", ""),
            ("TF_HTTP_LOCK_ADDRESS", "/lock"),
            ("TF_HTTP_UNLOCK_ADDRESS", "/lock"),
        ):
            address = str(env.get(key))
            if not address.endswith(f"/terraform/state/{root}{suffix}"):
                problems.append(
                    f"{root}: {key} does not end in /terraform/state/{root}{suffix}: {address!r}"
                )
    return problems


def check_terraform(render: Path, **_kw) -> None:
    """Canonical formatting, a parsable tailnet policy, and state backends in step.

    Terraform files are Jinja here, so a template bug lands as invalid HCL.
    Needs no network and no credentials.
    """
    tf = render / "terraform"
    if not tf.is_dir():
        raise Failure("the render has no terraform/")
    inspected = sorted(tf.rglob("*.tf"))
    if not inspected:
        raise Failure("terraform/ exists but ships no .tf file — `terraform fmt` checked nothing")
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
    if not modules:
        raise Failure(
            "no terraform/*/versions.tf in the render — the rendered HCL belongs to no "
            "module, so neither this check nor terraform-validate inspects a root"
        )
    state = tf_state_backend_problems(render)
    if state:
        raise Failure("rendered Terraform state backends have drifted:\n  " + "\n  ".join(state))
    print(
        f"  terraform fmt ok ({modules} modules, {len(inspected)} files, "
        f"{len(policies)} policy documents, state backends in step)"
    )


_MODULE_SOURCE = re.compile(r'"git::[^"]*//terraform/modules/([A-Za-z0-9_-]+)\?ref=[^"]*"')


def check_terraform_validate(
    render: Path, lib_path: Path | None = None, ref_verified: bool = True, **_kw
) -> None:
    """`terraform validate` per module against the library checkout: each
    `git::…?ref=<tag>` source is rewritten to the local checkout in a throwaway
    copy, with `-lockfile=readonly` as CI's plan and apply use."""
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
    names = ", ".join(m.name for m in modules)
    print(
        _lib_verdict(
            f"terraform validate ok ({names})",
            f"terraform validate ({names}) against the library checkout on disk, "
            "not the pinned ref",
            ref_verified,
        )
    )


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


def _kubeconform(render: Path, k8s_version: str, manifests: str, *extra: str):
    """One kubeconform invocation for every manifest corpus a render validates.

    `extra` carries the output shape only: the schema locations and the
    kubernetes version are shared, so no corpus is validated more loosely.
    """
    return _run(
        [
            "kubeconform",
            "-ignore-missing-schemas",
            "-kubernetes-version", k8s_version,
            "-schema-location", "default",
            "-schema-location", CRD_CATALOG,
            *extra,
            "-",
        ],
        input=manifests,
        cwd=render,
    )


def _skipped_kinds(render: Path, k8s_version: str, manifests: list[str]) -> list[str]:
    """Kinds kubeconform validated against NO schema, via the render's own
    tracker. kubeconform's own return code never shows them, so an unexpected
    pair is the caller's failure."""
    tracker = render / "scripts" / "kubeconform-skipped.py"
    if not tracker.is_file():
        raise Failure(
            "the render ships no scripts/kubeconform-skipped.py, so the kinds "
            "kubeconform validated against no schema went unreported"
        )
    audit = _kubeconform(
        render, k8s_version, "\n---\n".join(manifests), "-output", "json"
    )
    report = _run([sys.executable, str(tracker)], input=audit.stdout, cwd=render)
    # The tracker lists each skipped kind as "  - <group>/<Kind>"; everything
    # else it prints is prose.
    return [line.strip(" -") for line in report.stdout.splitlines() if line.startswith("  - ")]


# The wiring example under the tenants README's `## Example` heading — the
# Namespace, quota, secret store, GitRepository, ServiceAccount, RoleBindings and
# Kustomization an operator copies into a file of their own.
_TENANT_EXAMPLE = re.compile(r"^## Example\s*$.*?^```yaml\s*$(?P<body>.*?)^```\s*$", re.S | re.M)


def _tenant_example_problems(render: Path, k8s_version: str, values: dict[str, str]) -> list[str]:
    """Schema-validate the tenant wiring the README hands the operator.

    No Kustomization builds it, so kustomize and kubeconform never see it: a
    mis-nested key would surface at the operator's first apply.
    """
    readmes = sorted(render.glob("kubernetes/clusters/*/tenants/README.md"))
    if not readmes:
        return ["no kubernetes/clusters/*/tenants/README.md — the tenant wiring went unvalidated"]
    problems = []
    for readme in readmes:
        rel = readme.relative_to(render)
        match = _TENANT_EXAMPLE.search(readme.read_text())
        if not match:
            problems.append(
                f"{rel}: no yaml block under `## Example` — the wiring an operator "
                "pastes would ship unvalidated"
            )
            continue
        body = match.group("body")
        unknown = sorted({m for m in PLACEHOLDER.findall(body) if m not in values})
        if unknown:
            problems.append(f"{rel}: placeholders with no ConfigMap key: {unknown}")
            continue
        conform = _kubeconform(
            render,
            k8s_version,
            PLACEHOLDER.sub(lambda m: values[m.group(1)], body),
            "-strict",
            "-summary",
        )
        if conform.returncode:
            problems.append(f"kubeconform {rel} (## Example):\n{conform.stdout}{conform.stderr}")
    return problems


def check_flux(render: Path, **_kw) -> None:
    _need("kustomize")
    _need("kubeconform")
    configmaps = _configmap_paths(render)
    values = _substitutions(render, configmaps)

    ver = _run(
        ["bash", _render_script(render), "k8s-version", " ".join(configmaps)], cwd=render
    )
    if ver.returncode or not ver.stdout.strip():
        raise Failure(
            f"{_render_script(render)} k8s-version failed (rc={ver.returncode}): "
            + (ver.stderr.strip() or "empty output — no parseable k3s_version")
        )
    k8s_version = ver.stdout.strip()

    cluster_dirs = sorted((render / "kubernetes" / "clusters").glob("*"))
    if not cluster_dirs:
        raise Failure("no kubernetes/clusters/<name>/ in the render")

    failures: list[str] = []
    validated: list[str] = []
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
            if not build.stdout.strip():
                failures.append(f"{src}: kustomize build produced no resources")
                continue
            unknown = sorted({m for m in PLACEHOLDER.findall(build.stdout) if m not in values})
            if unknown:
                failures.append(
                    f"{src}: placeholders with no ConfigMap key (they render EMPTY): {unknown}"
                )
                continue
            rendered = PLACEHOLDER.sub(lambda m: values[m.group(1)], build.stdout)
            conform = _kubeconform(render, k8s_version, rendered, "-strict", "-summary")
            if conform.returncode:
                failures.append(f"kubeconform {src}:\n{conform.stdout}{conform.stderr}")
            validated.append(rendered)
            built += 1
    failures += _tenant_example_problems(render, k8s_version, values)
    if failures:
        raise Failure("\n".join(failures))
    if not validated:
        raise Failure("no Kustomization rendered — kubeconform validated nothing")
    skipped = _skipped_kinds(render, k8s_version, validated)
    if unexpected := sorted(set(skipped) - EXPECTED_SKIPPED):
        raise Failure(
            "kubeconform had no schema for " + ", ".join(unexpected) + " — those "
            "resources are UNVALIDATED. A failed catalog fetch looks the same as a "
            "new CRD here; add the pair to EXPECTED_SKIPPED once the gap is deliberate."
        )
    print(
        f"  flux ok ({built} Kustomizations + the tenant wiring example, k8s "
        f"{k8s_version}, {len(values)} substitutions"
        + (f", no schema for {', '.join(skipped)}" if skipped else "")
        + ")"
    )


# The wrapper the generated pipeline and Taskfile both call over the rendered
# corpus; running it here keeps the gate list from drifting into a third place.
# `netpol-parity` is separate: it reads the manifests on disk.
_CORPUS_GATE_WRAPPER = "flux-corpus-gates.sh"


def _prometheus_config(render: Path) -> str | None:
    """Why linting the alert rules and Alertmanager config failed, or None.

    kubeconform validates the HelmRelease schema and cannot see inside
    `.spec.values`, where every platform alert lives.
    """
    script = render / "scripts" / "lint-prometheus-config.sh"
    if not script.is_file():
        return "scripts/lint-prometheus-config.sh is wired into the Taskfile but not shipped"
    for tool in ("promtool", "amtool"):
        if not shutil.which(tool):
            return (
                f"{tool} is not on PATH: the alert-rule and Alertmanager validation "
                "cannot run, and it must not silently skip (pass --skip cluster-gates "
                "to opt out deliberately)"
            )
    result = _run(
        ["bash", "scripts/lint-prometheus-config.sh"],
        cwd=render,
        env=dict(os.environ, RULE_TESTS_DIR="tests/prometheus-rules"),
    )
    if result.returncode:
        return f"lint-prometheus-config.sh:\n{result.stdout}{result.stderr}"
    return None


def check_cluster_gates(render: Path, **_kw) -> None:
    """Run the shipped invariant gates over the shipped manifests.

    The render suite asserts the pipeline WIRES these; this asserts the payload
    passes them, so a generated cluster's first pipeline is not red on arrival.
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
    wrapper = render / "scripts" / _CORPUS_GATE_WRAPPER
    if not wrapper.is_file():
        failures.append(f"{_CORPUS_GATE_WRAPPER} is wired into CI but not shipped")
    else:
        # The wrapper takes a file, and its last arm needs the network, so only
        # the corpus gates run here.
        corpus_file = render / ".render-corpus.yaml"
        corpus_file.write_text(stream)
        try:
            result = _run(["bash", f"scripts/{_CORPUS_GATE_WRAPPER}", str(corpus_file)], cwd=render)
        finally:
            corpus_file.unlink(missing_ok=True)
        if result.returncode:
            failures.append(f"{_CORPUS_GATE_WRAPPER}:\n{result.stdout}{result.stderr}")

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

    prometheus = _prometheus_config(render)
    if prometheus:
        failures.append(prometheus)

    if failures:
        raise Failure("\n".join(failures))
    print(f"  cluster gates ok ({_CORPUS_GATE_WRAPPER} + 2 over {len(corpus)} builds)")


_SITE_DATA_SUFFIXES = {".yml", ".yaml", ".env", ".conf", ".toml"}

_LOCAL_HELPERS_HEADING = "## Local helpers"


def render_local_scripts(scripts: Path) -> set[str]:
    """Scripts a generated cluster owns, read from its own scripts/README.md.

    The README's "Local helpers" table is the declaration; every co-located
    test_*.py travels with the helper it drives.
    """
    readme = scripts / "README.md"
    if not readme.is_file():
        raise Failure(f"{readme} does not exist — the local-helper list cannot be read")
    local = md_tables.table_names(readme.read_text(encoding="utf-8"), _LOCAL_HELPERS_HEADING)
    if not local:
        raise Failure(
            f"{readme} has no '{_LOCAL_HELPERS_HEADING}' table — every script would "
            "read as an undeclared orphan"
        )
    local.update(p.name for p in scripts.glob("test_*.py"))
    return local


def _orphaned_scripts(
    scripts: Path, lib_scripts: Path, local: set[str], site_data: set[str]
) -> list[str]:
    """Problems for one scripts/ directory: files the library does not ship.

    `local` names files with no library twin, `site_data` the configuration a
    vendored tool reads. Byte-identity belongs to the manifest engine.
    """
    inspected = [path for path in sorted(scripts.iterdir()) if path.is_file()]
    if not inspected:
        return [f"{scripts}: holds no file — the orphan scan examined nothing"]
    orphaned = [
        path.name
        for path in inspected
        if path.name not in local
        and path.name != "README.md"
        and path.suffix not in _SITE_DATA_SUFFIXES
        and path.name not in site_data
        and not (lib_scripts / path.name).is_file()
    ]
    if not orphaned:
        return []
    return [
        f"{scripts}: holds files the library does not ship, and they are not "
        "declared local: " + ", ".join(orphaned)
    ]


def check_vendored(
    render: Path,
    lib_path: Path | None = None,
    self_checks: bool = False,
    ref_verified: bool = True,
    **_kw,
) -> None:
    """Every script with a library twin is a registered copy, byte-identical at
    the pinned ref; no script is an undeclared orphan. See lib docs/SCRIPTS.md.

    The scripts/ arms read no render, so they run once under --self-checks.
    """
    if not lib_path:
        raise Failure("--lib-path is required to check the vendored copies")
    lib_scripts = lib_path / "scripts"
    if not lib_scripts.is_dir():
        raise Failure(f"{lib_scripts} does not exist (is --lib-path a weisssrv-lib checkout?)")

    problems = _orphaned_scripts(
        render / "scripts",
        lib_scripts,
        # Scripts a generated cluster owns: they read its inventory and its
        # cluster-config, so they have no library twin.
        local=render_local_scripts(render / "scripts"),
        site_data={"version-registry.py"},
    )
    if self_checks:
        own_scripts = render_cluster.REPO_ROOT / "scripts"
        if own_scripts.is_dir():
            problems += _orphaned_scripts(own_scripts, lib_scripts, local=set(), site_data=set())
        # An ambiguous pin makes the manifest comparison meaningless, so it
        # REPLACES it — reporting drift against a ref the copies never came from
        # would send whoever reads this to re-copy the wrong file.
        mismatch = _assert_one_lib_ref()
        problems += mismatch or _check_registered_copies(lib_path)
    if problems:
        raise Failure("\n".join(problems))
    print(_vendored_row(self_checks, ref_verified))


def _vendored_row(self_checks: bool, ref_verified: bool) -> str:
    """CRITICAL: byte-identity is a claim about the PINNED ref. The engine falls
    back to the library working tree when that ref does not resolve, so the
    unverified spelling REPLACES the claim rather than qualifying it."""
    if not self_checks:
        return _lib_verdict(
            "vendored ok (render scripts only)",
            "vendored (ref unverified): no orphan scripts in the render",
            ref_verified,
        )
    return _lib_verdict(
        "vendored ok (registered copies byte-identical at the pin, no orphan scripts)",
        "vendored (ref unverified): no orphan scripts, and the registered copies match "
        "the library checkout on disk — not the pinned ref",
        ref_verified,
    )


MANIFEST_RELPATH = "scripts/vendored-manifest.yml"
_OFFER_RELPATH = "scripts/vendorable-paths.yml"


def _offered_script_names(lib_path: Path, ref: str) -> set[str]:
    """Basenames the library OFFERS for vendoring at `ref`.

    Read at the pin, not the working tree: a written-but-unreleased gate cannot
    be registered yet. Falls back to the working tree when the ref is absent.
    """
    blob = _run(["git", "-C", str(lib_path), "show", f"{ref}:{_OFFER_RELPATH}"])
    raw = blob.stdout if blob.returncode == 0 else ""
    if not raw:
        source = lib_path / _OFFER_RELPATH
        raw = source.read_text() if source.is_file() else ""
    offered = (yaml.safe_load(raw) or {}).get("vendorable") or []
    return {Path(path).name for path in offered if str(path).startswith("scripts/")}


def _own_ci() -> dict:
    """This repository's own pipeline, or an empty document when it ships none.

    The self-check arms read it; a bare read would raise out of the validator
    instead of letting each arm report its own missing-pin finding.
    """
    own = render_cluster.REPO_ROOT / ".gitlab-ci.yml"
    return render_cluster.load_ci(own) if own.is_file() else {}


def _check_registered_copies(lib_path: Path) -> list[str]:
    """Run the library's vendored-copy engine over this repository's manifest.

    The manifest is the authority on every copy relationship, forks included,
    and the comparison happens at the ref this repository pins.
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
    ref = (_own_ci().get("variables") or {}).get("WEISSSRV_LIB_REF")
    if not ref:
        return [
            "this repository's .gitlab-ci.yml declares no variables.WEISSSRV_LIB_REF — "
            f"{MANIFEST_RELPATH} would be compared against whatever the library "
            "checkout happens to hold, not the ref the copies were taken at"
        ]
    argv = [
        sys.executable,
        str(checker),
        "--manifest",
        str(manifest),
        "--repo-root",
        str(render_cluster.REPO_ROOT),
        "--lib-path",
        str(lib_path),
    ]
    problems = []
    result = _run(argv + ["--ref", str(ref)])
    if result.returncode:
        # The engine falls back to the working tree when the ref does not
        # resolve, so a failure here is drift against whichever tree it names.
        # Copies matching that tree under an older pin need a bump, not a re-vendor.
        hint = ""
        if _run(argv).returncode == 0:
            hint = (
                "\nThese copies match the library's working tree exactly, but this "
                f"repository pins {ref}. That is the expected state while a library tag "
                "is being cut; resolve it by bumping copier.yml's lib_ref default (and "
                "this repository's own includes) once the tag exists — never by "
                "re-vendoring backwards."
            )
        problems.append(
            f"vendored copies ({MANIFEST_RELPATH}):\n{result.stdout}{result.stderr}{hint}"
        )
    problems += _unregistered_twins(argv, lib_path, str(ref))
    return problems


def _unregistered_twins(argv: list[str], lib_path: Path, ref: str) -> list[str]:
    """This repository's scripts/ may hold no library twin the manifest omits.

    The engine validates what the manifest names and cannot see what it leaves
    out, so an unregistered copy is held to nothing.
    """
    listed = _run(argv + ["--list"])
    if listed.returncode:
        return [f"{MANIFEST_RELPATH} does not parse:\n{listed.stdout}{listed.stderr}"]
    registered = {
        Path(parts[1]).name
        for parts in (line.split("\t") for line in listed.stdout.splitlines())
        if len(parts) >= 3
    }
    if not registered:
        return [
            f"{MANIFEST_RELPATH} registers no copy — the comparison above examined "
            "nothing, so every library twin in this repository is ungated"
        ]
    own = render_cluster.REPO_ROOT / "scripts"
    if not own.is_dir():
        return []
    lib_names = _offered_script_names(lib_path, ref)
    if not lib_names:
        return [f"the library offers no scripts at {ref} — this arm examined nothing"]
    unregistered = sorted(
        path.name
        for path in own.iterdir()
        if path.is_file()
        and path.name in lib_names
        and path.name not in registered
        and path.suffix not in _SITE_DATA_SUFFIXES
    )
    if not unregistered:
        return []
    return [
        f"this repository's scripts/ carries library twins {MANIFEST_RELPATH} does not "
        f"name: {', '.join(unregistered)} — nothing holds them to the library. Add them "
        "to scripts/vendored-manifest.yml."
    ]


RENDER_GATE_RELPATH = "tests/test_vendored_byte_identity.py"


def check_rendered_vendored(
    render: Path, lib_path: Path | None = None, ref_verified: bool = True, **_kw
) -> None:
    """The render's own vendored-copy gate passes on the render: the library
    engine runs over the render's manifest at the render's pin, every rendered
    script with a library twin is registered, and the gate script renders."""
    if not lib_path:
        raise Failure("--lib-path is required to run the render's vendored-copy gate")
    checker = lib_path / "scripts" / "check-vendored-copies.py"
    if not checker.is_file():
        raise Failure(
            f"{lib_path} ships no scripts/check-vendored-copies.py — the render's "
            "vendored-copy gate cannot run, and it must not silently skip"
        )
    manifest = render / MANIFEST_RELPATH
    if not manifest.is_file():
        raise Failure(
            f"the render ships no {MANIFEST_RELPATH} — every copy it carries from the "
            "library would go ungated in the generated repository"
        )
    if not (render / RENDER_GATE_RELPATH).is_file():
        raise Failure(
            f"the render ships {MANIFEST_RELPATH} but no {RENDER_GATE_RELPATH} — nothing "
            "in the generated repository reads the manifest, so the copies are ungated "
            "there however correct the list is"
        )

    ref = (render_cluster.load_ci(render / ".gitlab-ci.yml").get("variables") or {}).get(
        "WEISSSRV_LIB_REF"
    )
    if not ref:
        raise Failure(
            "the render declares no variables.WEISSSRV_LIB_REF — the shipped gate reads "
            "its pin from there, so it would have nothing to compare at"
        )

    problems: list[str] = []
    argv = [
        sys.executable,
        str(checker),
        "--manifest",
        str(manifest),
        "--repo-root",
        str(render),
        "--lib-path",
        str(lib_path),
    ]
    result = _run(argv + ["--ref", str(ref)])
    if result.returncode:
        # The engine falls back to the working tree when the ref does not
        # resolve, so a failure here is drift against whichever tree it names.
        # Copies matching that tree under an older pin need a bump, not a re-vendor.
        hint = ""
        if _run(argv).returncode == 0:
            hint = (
                f"\nThese copies match the library's working tree exactly, but the render "
                f"pins {ref}. That is the expected state while a library tag is being cut; "
                f"resolve it by bumping copier.yml's lib_ref default (and this "
                f"repository's own includes) once the tag exists — never by re-vendoring "
                f"backwards."
            )
        problems.append(f"the render's {MANIFEST_RELPATH}:\n{result.stdout}{result.stderr}{hint}")

    listed = _run(argv + ["--list"])
    if listed.returncode:
        problems.append(f"the render's {MANIFEST_RELPATH} does not parse:\n{listed.stderr}")
        registered: set[str] = set()
    else:
        registered = {
            Path(parts[1]).name
            for parts in (line.split("\t") for line in listed.stdout.splitlines())
            if len(parts) >= 3
        }

    lib_names = _offered_script_names(lib_path, str(ref))
    if not lib_names:
        problems.append(f"the library offers no scripts at {ref} — this arm examined nothing")
    unregistered = sorted(
        path.name
        for path in sorted((render / "scripts").iterdir())
        if path.is_file()
        and path.name in lib_names
        and path.name not in registered
        and path.suffix not in _SITE_DATA_SUFFIXES
    )
    if unregistered:
        problems.append(
            f"the render's scripts/ carries library twins that its {MANIFEST_RELPATH} does "
            f"not name: {', '.join(unregistered)} — they ship to every generated cluster "
            "ungated. Add them to template/scripts/vendored-manifest.yml."
        )

    if not registered:
        problems.append(
            f"the render's {MANIFEST_RELPATH} registers no copy — every library file it "
            "ships would go ungated in the generated repository"
        )

    if problems:
        raise Failure("\n".join(problems))
    print(
        _lib_verdict(
            f"rendered vendored gate ok ({len(registered)} copies registered by the render)",
            f"rendered vendored gate (ref unverified): {len(registered)} copies registered, "
            "compared against the library checkout on disk",
            ref_verified,
        )
    )


class GitUnavailable(Exception):
    """git could not be asked what the library checkout is at."""


def _pinned_lib_ref() -> str:
    """The library ref every lib-reading gate here has as its subject.

    The fixtures inherit lib_ref from copier.yml's default (the single source),
    so that default is the ref render-validate clones.
    """
    root = render_cluster.REPO_ROOT
    return yaml.safe_load((root / "copier.yml").read_text())["lib_ref"]["default"]


def _checkout_tags(lib_path: Path) -> list[str]:
    """Every tag the library checkout's HEAD carries.

    A release commit may carry more than one, so a caller tests membership
    rather than reading the first entry.
    """
    try:
        result = _run(["git", "-C", str(lib_path), "tag", "--points-at", "HEAD"])
    except OSError as error:
        raise GitUnavailable(str(error)) from error
    if result.returncode:
        raise GitUnavailable(result.stderr.strip() or f"git exited {result.returncode}")
    return result.stdout.split()


def _checkout_dirty(lib_path: Path) -> list[str]:
    """Up to three tracked paths modified in the library checkout.

    A checkout sitting on the pinned tag with uncommitted edits serves
    unreleased files; a git failure is not "clean".
    """
    try:
        result = _run(["git", "-C", str(lib_path), "status", "--porcelain", "--untracked-files=no"])
    except OSError as error:
        raise GitUnavailable(str(error)) from error
    if result.returncode:
        raise GitUnavailable(result.stderr.strip() or f"git exited {result.returncode}")
    return [line[3:] for line in result.stdout.splitlines()][:3]


def lib_checkout_problems(lib_path: Path, expected: str) -> list[str]:
    """CRITICAL: --lib-path may be any tree. Without this, the vendored, role
    and include gates compare the render against an unrelated library and report
    green. A git failure is reported as unverified, never as a match."""
    try:
        tags = _checkout_tags(lib_path)
        if expected not in tags:
            return [
                f"library checkout {lib_path} is at {sorted(tags) or 'no tag'}, not the "
                f"pinned {expected} — every gate reading library files would have "
                "another library tree as its subject. Pass --allow-ref-mismatch to "
                "validate against an unreleased checkout anyway."
            ]
        dirty = _checkout_dirty(lib_path)
    except GitUnavailable as error:
        return [f"cannot read {lib_path}'s git state, so the pinned ref is unverified: {error}"]
    if dirty:
        return [
            f"library checkout {lib_path} is at {expected} but has uncommitted changes "
            f"to {', '.join(dirty)} — library files would be read unreleased"
        ]
    return []


def _assert_one_lib_ref() -> list[str]:
    """The template's own pipeline must pin the ref the fixtures resolve to.

    One library checkout gates both trees, so includes on another tag would have
    the comparison above run against the wrong ref.
    """
    expected = _pinned_lib_ref()
    refs = {
        inc["ref"]
        for inc in _own_ci().get("include", [])
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

    Every task in such a role is gated on the flag, so invoking it without
    setting the flag runs a play that does nothing, successfully.
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

    for path in _yaml_paths(playbooks_dir):
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


def check_role_opt_ins(
    render: Path, lib_path: Path | None = None, ref_verified: bool = True, **_kw
) -> None:
    """An opt-in role invoked unconditionally must have its flag set in the
    inventory, or the play reports ok with every task skipped. An invocation
    guarded by the flag itself is the deliberate off-unless-set case.
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
    for path in _yaml_paths(inventory):
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
    print(
        _lib_verdict(
            f"role opt-ins ok ({checked} invocations of {len(opt_in)} opt-in roles)",
            f"role opt-ins (ref unverified): {checked} invocations of {len(opt_in)} opt-in "
            "roles, read from the library checkout on disk",
            ref_verified,
        )
    )


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


class _Unmodelled(Exception):
    """An expression needs an Ansible behaviour this evaluator does not carry."""


_ENV_SUPPLIED = "<supplied from the environment>"


def _env_lookup(name, *_args, **_kwargs):
    """Ansible's `lookup()`, limited to the env plugin the collection's defaults
    use: an input defaulted to an env lookup is supplied outside the inventory,
    so it counts as given. Any other lookup is unmodelled."""
    if str(name).rsplit(".", 1)[-1] != "env":
        raise _Unmodelled(f"lookup({name!r}, ...)")
    return _ENV_SUPPLIED


def _jinja_env():
    """Jinja carrying the Ansible-only filters, tests and lookups the
    collection's `when:` expressions and defaults use. Anything else raises, and
    the caller records it rather than guessing an answer."""
    import jinja2

    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)  # noqa: S701
    env.filters["bool"] = _ansible_bool
    env.filters["from_json"] = json.loads
    env.tests["match"] = lambda value, pattern: bool(re.match(pattern, str(value)))
    env.tests["search"] = lambda value, pattern: bool(re.search(pattern, str(value)))
    env.globals["lookup"] = _env_lookup
    return env


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


def _reachable_by_default(
    conditions: tuple[str, ...],
    defaults: dict,
    unmodelled: list[str] | None = None,
    source: str = "",
) -> bool | None:
    """Would this task run on a host that sets none of the role's own inputs?

    None when the expression is outside what this evaluator models; it goes to
    `unmodelled`, because that answer silently drops the role's asserted inputs.
    """
    env = _jinja_env()
    for condition in conditions:
        try:
            verdict = env.from_string(
                "{% if " + condition + " %}yes{% else %}no{% endif %}"
            ).render(**defaults)
        except Exception as exc:  # noqa: BLE001 - recorded, not guessed
            if unmodelled is not None:
                unmodelled.append(
                    f"{source}: when {condition!r} is outside this evaluator: {exc}"
                )
            return None
        if verdict != "yes":
            return False
    return True


def _asserted_inputs(
    role: Path, defaults: dict, unmodelled: list[str] | None = None
) -> set[str]:
    """Role-prefixed variables the role asserts non-empty on its DEFAULT path.

    `<role_name>_*` only, `assert` tasks only, and only asserts reachable with
    nothing set: an opt-in feature's assert is a contract, not a requirement.
    """
    found: set[str] = set()
    tasks_dir = role / "tasks"
    for path in _yaml_paths(tasks_dir) if tasks_dir.is_dir() else []:
        where = f"{role.name}/{path.relative_to(role).as_posix()}"
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            if unmodelled is not None:
                unmodelled.append(f"{where}: unparseable, its asserts were not scanned: {exc}")
            continue
        for task, conditions in _walk_tasks(doc):
            spec = task.get("ansible.builtin.assert") or task.get("assert")
            if not isinstance(spec, dict):
                continue
            if _reachable_by_default(conditions, defaults, unmodelled, where) is not True:
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


def _render_default(
    value, context: dict, unmodelled: list[str] | None = None, source: str = ""
):
    """The value a role default takes on an inventory holding `context`, with
    whole-template results converted back to native types as Ansible does.
    `_UNMODELLED`, and a line in `unmodelled`, when it cannot be rendered."""
    if not isinstance(value, str) or "{{" not in value:
        return value
    try:
        rendered = _jinja_env().from_string(value).render(**context)
    except Exception as exc:  # noqa: BLE001 - recorded, not guessed
        if unmodelled is not None:
            unmodelled.append(f"{source} is outside this evaluator: {exc}")
        return _UNMODELLED
    try:
        return ast.literal_eval(rendered)
    except (ValueError, SyntaxError):
        return rendered


def _referenced_names(
    value, unmodelled: list[str] | None = None, source: str = ""
) -> set[str]:
    """Inventory variables a default's expression reads."""
    if not isinstance(value, str) or "{{" not in value:
        return set()
    from jinja2 import meta

    try:
        return meta.find_undeclared_variables(_jinja_env().parse(value))
    except Exception as exc:  # noqa: BLE001 - recorded, not guessed
        if unmodelled is not None:
            unmodelled.append(f"{source} does not parse: {exc}")
        return set()


def _is_empty(value) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value in (None, [], {}, ())


def _default_gap(
    defaults: dict,
    var: str,
    context: dict,
    assigned: set[str],
    unmodelled: list[str] | None = None,
    role_name: str = "",
) -> str | None:
    """None when defaults/main.yml gives `var` a non-empty value once rendered
    against this inventory; otherwise the reason it does not. Two gaps: the key
    is absent, or its expression renders empty.
    """
    if var not in defaults:
        return "gives it no default in defaults/main.yml"
    raw = defaults[var]
    source = f"{role_name}: the default for {var}"
    value = _render_default(raw, context, unmodelled, source)
    if value is _UNMODELLED or not _is_empty(value):
        return None
    if (_referenced_names(raw, unmodelled, source) & assigned) - set(context):
        return None
    if isinstance(raw, str) and "{{" in raw:
        return f"defaults it to {raw!r}, which renders EMPTY against this inventory"
    return f"defaults it to {raw!r}, which is empty"


def check_required_role_inputs(
    render: Path, lib_path: Path | None = None, ref_verified: bool = True, **_kw
) -> None:
    """A role input the role asserts and has no usable default for must be set
    in the inventory. "Usable" is decided by RENDERING the default (see
    `_default_gap`). Static: it reads the library, it does not replay a play.
    """
    if not lib_path:
        raise Failure("--lib-path is required to read the roles' required inputs")
    roles_dir = lib_path / "ansible_collections" / "weisssrv" / "infra" / "roles"
    if not roles_dir.is_dir():
        raise Failure(f"{roles_dir} does not exist (is --lib-path a weisssrv-lib checkout?)")

    inventory = render / "ansible" / "inventories" / "prod"
    assigned: set[str] = set()
    for path in _yaml_paths(inventory):
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            assigned.update(_ASSIGNMENT.findall(line))

    # Group vars as values, not just names: a role feature flag can default
    # differently in the library and here, so an assert's `when:` is only
    # judgeable with the inventory's answer in hand.
    group_values: dict = {}
    for path in _yaml_paths(inventory / "group_vars", recursive=False):
        doc = yaml.safe_load(path.read_text())
        if isinstance(doc, dict):
            group_values.update(doc)

    invoked = {
        role.rsplit(".", 1)[-1]: path
        for path, role, _when in _role_invocations(render / "ansible" / "playbooks")
        if role
    }
    required, problems = 0, []
    unmodelled: list[str] = []
    for name, playbook in sorted(invoked.items()):
        role = roles_dir / name
        if not role.is_dir():
            continue
        defaults = _role_defaults(role)
        context = {**defaults, **group_values}
        for var in sorted(_asserted_inputs(role, context, unmodelled)):
            gap = _default_gap(defaults, var, context, assigned, unmodelled, name)
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
    if unmodelled:
        raise Failure(
            "these expressions are outside this evaluator, so the inputs behind them "
            "left the required set unchecked — teach _jinja_env() the behaviour they "
            "need:\n" + "\n".join(sorted(set(unmodelled)))
        )
    if not required:
        raise Failure(
            "no invoked role declares an asserted input without a usable default — "
            "either the collection dropped the convention or the assert scan is "
            "stale; this check is now examining nothing"
        )
    if problems:
        raise Failure("\n".join(sorted(set(problems))))
    print(
        _lib_verdict(
            f"required role inputs ok ({required} asserted inputs with no usable default "
            "assigned, every expression modelled)",
            f"required role inputs (ref unverified): {required} asserted inputs assigned, "
            "read from the library checkout on disk",
            ref_verified,
        )
    )


def check_inventory_addresses(render: Path, **_kw) -> None:
    """cluster-config must declare the LAN and all three VIPs in the fixtures.

    The address invariants themselves ship in the render's own
    tests/test_cluster_invariants.py, which SKIPS when cluster-config is silent.
    """
    required = {
        "cluster_lan_cidr": "the LAN CIDR",
        "cluster_api_vip": "the k3s API VIP",
        "cluster_metallb_public_vip": "the public MetalLB VIP",
        "cluster_metallb_internal_vip": "the internal MetalLB VIP",
    }
    config = _cluster_config(render)
    if not config:
        raise Failure(
            "kubernetes/infrastructure/sources/cluster-config.yaml declares no data — "
            "every substituted manifest renders empty"
        )
    missing = sorted(f"{key} ({label})" for key, label in required.items() if not config.get(key))
    if missing:
        raise Failure(
            "cluster-config declares no " + ", ".join(missing) + " — the render's own "
            "inventory-address invariants skip instead of asserting"
        )
    print(f"  inventory addresses ok (cluster-config declares {len(required)} site values)")


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


def check_version_coverage(render: Path, **_kw) -> None:
    """Every pin in the rendered vars file has a version-registry entry.

    Both are template output, so an entry added to one .jinja and not the other
    ships a cluster whose bump bot never reports that pin.
    """
    checker = render / "scripts" / "check-versions.py"
    if not checker.is_file():
        raise Failure(f"{checker} is missing from the render")
    result = _run([sys.executable, str(checker), "--check-coverage"], cwd=render)
    if result.returncode:
        raise Failure(result.stdout + result.stderr)
    print("  version coverage ok (every pin has a registry entry)")


def check_versions_configmap(render: Path, **_kw) -> None:
    """The rendered cluster-versions ConfigMap matches the rendered vars file.

    Separate .jinja files, so a pin bumped in one renders a cluster that fails
    its own `task lint:repo-sync`. check_flux substitutes FROM the ConfigMap.
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


_VARIABLE_REF = re.compile(r"^\$\{?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}?$")


def _resolve(name: str, variables: dict) -> str:
    """A job image written as `$VAR` takes its value from the pipeline's own
    `variables:` block, so the pin lives there rather than on the job."""
    match = _VARIABLE_REF.match(name.strip())
    if not match:
        return name
    value = variables.get(match.group("name"))
    return value if isinstance(value, str) else name


def _unpinned(image, variables: dict | None = None) -> str | None:
    """Why this image is not pinned, or None when it is."""
    name = _image_name(image)
    if name is None:
        return f"unreadable image reference {image!r}"
    name = _resolve(name, variables or {})
    if _VARIABLE_REF.match(name.strip()):
        return f"{name} is not defined in the pipeline's variables:, so its image is unknown"
    tag = name.rsplit("/", 1)[-1]
    if ":" not in tag:
        return f"{name} carries no tag, so it resolves to whatever :latest is today"
    if tag.rsplit(":", 1)[-1] == "latest":
        return f"{name} pins :latest, which is not a pin"
    return None


def check_ci_policy(render: Path, **_kw) -> None:
    """The rendered pipeline sets default.image, default.interruptible: true,
    workflow.auto_cancel (main: none, live-infrastructure jobs: false), and
    every playbook-running deploy job triggers on the collection pin.
    """
    ci = render_cluster.load_ci(render / ".gitlab-ci.yml")
    problems: list[str] = []
    variables = ci.get("variables") or {}

    default = ci.get("default") or {}
    if not isinstance(default, dict) or "image" not in default:
        problems.append("no top-level `default: image:` — every job's image is the runner's choice")
    else:
        why = _unpinned(default["image"], variables)
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
            why = _unpinned(body["image"], variables)
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

    # The render ships this gate but no rendered CI job runs it, so drift in a
    # deploy job's `changes:` list would otherwise only surface in `task lint`.
    gate = render / "scripts" / "check-collection-pin-trigger.py"
    if not gate.is_file():
        problems.append("the render ships no scripts/check-collection-pin-trigger.py")
    else:
        done = _run([sys.executable, str(gate)], cwd=render)
        if done.returncode:
            problems.append(
                "check-collection-pin-trigger.py: " + (done.stdout + done.stderr).strip()
            )

    if problems:
        raise Failure("\n".join(problems))
    print(
        f"  ci policy ok (pinned default image, auto_cancel split, {len(live)} "
        "uninterruptible live-infrastructure jobs, collection-pin trigger coverage)"
    )


_INPUT_REF = re.compile(r"^\$\[\[\s*inputs\.([A-Za-z0-9_-]+)\s*\]\]$")

# Keys a library template's job document may hold that are not jobs. Without the
# global keywords a mapping here reads as a stage-less job.
_NOT_A_JOB = {
    "include", "variables", "stages", "workflow", "default", "image", "spec",
    "cache", "services", "before_script", "after_script", "pages",
}

# GitLab's default stage for a job that declares none. Resolving it to nothing
# would hide such a job from the stage arm, and `test` is the stage a pipeline
# with custom `stages:` is most likely not to declare.
_IMPLICIT_STAGE = "test"


def _resolve_input(value, inputs: dict, passed: dict):
    """Resolve `$[[ inputs.x ]]` against what the consumer passed, else the default."""
    match = _INPUT_REF.match(value) if isinstance(value, str) else None
    if not match:
        return value
    name = match.group(1)
    if name in passed:
        return passed[name]
    return (inputs.get(name) or {}).get("default")


def include_contract_problems(pipeline: Path, lib_path: Path) -> "tuple[int, list[str]]":
    """(library includes inspected, problems) for one pipeline file.

    An undeclared `inputs:` key and a default-less input nobody passes both fail
    pipeline creation; a job resolving to an undeclared stage does too.
    """
    ci = render_cluster.load_ci(pipeline)
    declared = ci.get("stages")
    # No `stages:` key means GitLab's implicit defaults; .pre/.post always exist.
    stages = set(declared if declared is not None else ("build", "test", "deploy"))
    stages.update({".pre", ".post"})
    includes = ci.get("include") or []
    if isinstance(includes, dict):
        includes = [includes]
    inspected = 0
    problems: list[str] = []
    for include in includes:
        if not isinstance(include, dict) or "project" not in include:
            continue
        rel = str(include.get("file", "")).lstrip("/")
        source = lib_path / rel
        if not source.is_file():
            problems.append(f"{rel} is not in the library checkout")
            continue
        inspected += 1
        docs = [d for d in yaml.load_all(source.read_text(), Loader=render_cluster.CILoader) if d]
        # The `spec:` header is optional: a template with no inputs is legal and
        # its first document already holds jobs.
        header = docs[0] if docs and isinstance(docs[0], dict) and "spec" in docs[0] else None
        inputs = ((header or {}).get("spec") or {}).get("inputs") or {}
        job_docs = docs[1:] if header is not None else docs
        passed = include.get("inputs") or {}
        for key in passed:
            if key not in inputs:
                problems.append(f"{rel} declares no input {key!r}")
        for key, declaration in inputs.items():
            if key not in passed and "default" not in (declaration or {}):
                problems.append(f"{rel} requires input {key!r}, but none is passed")
        for doc in job_docs:
            for name, body in doc.items():
                if name in _NOT_A_JOB or name.startswith(".") or not isinstance(body, dict):
                    continue
                if "stage" in body:
                    stage = _resolve_input(body["stage"], inputs, passed)
                elif "extends" in body:
                    # The stage comes from the extended job, possibly in another
                    # file; resolving that chain is out of scope.
                    continue
                else:
                    stage = _IMPLICIT_STAGE
                if stage is not None and stage not in stages:
                    job = _resolve_input(name, inputs, passed)
                    problems.append(
                        f"{rel}: job {job!r} resolves to stage {stage!r}, which "
                        f"{pipeline.name} does not declare"
                    )
    return inspected, problems


def check_include_contract(
    render: Path,
    lib_path: Path | None = None,
    self_checks: bool = False,
    ref_verified: bool = True,
    **_kw,
) -> None:
    """Every library include resolves, with each job's stage present.

    Inputs must be declared and the required ones passed. A default-less input
    added upstream would otherwise first surface as a pipeline-creation failure.
    """
    if not lib_path:
        raise Failure("--lib-path is required to check the include contract")
    inspected = 0
    problems: list[str] = []
    pipelines = [("render", render / ".gitlab-ci.yml")]
    if self_checks:
        # No render is read here, so this arm runs once per pipeline.
        pipelines.append(("template", render_cluster.REPO_ROOT / ".gitlab-ci.yml"))
    for label, pipeline in pipelines:
        if not pipeline.is_file():
            problems.append(f"{label}: {pipeline} is missing, so its includes went unchecked")
            continue
        found, issues = include_contract_problems(pipeline, lib_path)
        inspected += found
        problems += [f"{label}: {issue}" for issue in issues]
    if not inspected and not problems:
        problems.append("no library include was inspected — this gate examined nothing")
    if problems:
        raise Failure("\n".join(problems))
    print(
        _lib_verdict(
            f"include contract ok ({inspected} library includes resolved at the pin)",
            f"include contract (ref unverified): {inspected} includes resolved against the "
            "library checkout on disk, not the pinned ref",
            ref_verified,
        )
    )


def check_ansible(
    render: Path, lib_path: Path | None = None, workdir: Path | None = None, **_kw
) -> None:
    _need("ansible-playbook")
    ansible_dir = render / "ansible"
    playbooks = _yaml_paths(ansible_dir / "playbooks", recursive=False) if ansible_dir.is_dir() else []
    if not playbooks:
        raise Failure("the render ships no ansible/playbooks/*.yml or *.yaml")

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
        deps = _run(
            ["ansible-galaxy", "collection", "install", "-r", str(filtered), "-p", str(dest)],
            cwd=ansible_dir,
            env=env,
        )
        if deps.returncode:
            # Not fatal: the offline fallback below can still resolve them, but
            # silence here is what lets a run pass locally and fail in CI.
            print(
                "  WARNING: ansible-galaxy dependency install failed, falling back "
                "to ~/.ansible/collections:\n" + deps.stdout + deps.stderr
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


# The check registry, module-level so the --skip help, docs/CI.md and the test
# that holds them equal all read the same list. `needs` marks the checks that
# require --lib-path; without it the run fails unless --skip names them.
CHECKS = (
    ("yamllint", check_yamllint, frozenset()),
    ("shellcheck", check_shellcheck, frozenset()),
    ("terraform", check_terraform, frozenset()),
    ("flux", check_flux, frozenset()),
    ("cluster-gates", check_cluster_gates, frozenset()),
    ("ci-policy", check_ci_policy, frozenset()),
    ("include-contract", check_include_contract, frozenset({"lib"})),
    ("inventory-addresses", check_inventory_addresses, frozenset()),
    ("version-coverage", check_version_coverage, frozenset()),
    ("versions-configmap", check_versions_configmap, frozenset()),
    ("vendored", check_vendored, frozenset({"lib"})),
    ("rendered-vendored", check_rendered_vendored, frozenset({"lib"})),
    ("role-opt-ins", check_role_opt_ins, frozenset({"lib"})),
    ("role-inputs", check_required_role_inputs, frozenset({"lib"})),
    ("terraform-validate", check_terraform_validate, frozenset({"lib"})),
    ("ansible", check_ansible, frozenset()),
)

CHECK_NAMES = tuple(name for name, _, _ in CHECKS)


# Every top-level area a render ships, mapped to the checks above that inspect
# it or the reason none does. tests/test_render.py holds a real render's layout
# to this map, so a new rendered subtree cannot arrive silently unchecked.
RENDERED_AREAS = {
    "ansible": "ansible, role-opt-ins, role-inputs, inventory-addresses, yamllint",
    "kubernetes": "flux, cluster-gates, versions-configmap, yamllint",
    "terraform": "terraform, terraform-validate, shellcheck",
    "scripts": "shellcheck, vendored, rendered-vendored, version-coverage, yamllint",
    "lint": "yamllint, which lints with the render's own profiles from this directory",
    "taskfiles": "no check here: the Taskfile tree is held by the render suite's "
    "task gates, which read it as YAML rather than running it",
    "tests": "no check here: the generated repository's own suite runs in its pipeline",
    "docs": "no check here: prose, held by the render suite's doc and link gates",
    ".gitlab": "ci-policy, include-contract, through the pipeline that includes "
    "these job files",
    ".claude": "no check here: agent instructions, held by the render suite's doc gates",
    ".cursor": "no check here: agent instructions, held by the render suite's doc gates",
    ".ansible": "no check here: empty collection and role install targets",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path, help="Validate this render instead of a new one.")
    parser.add_argument("--answers", type=Path, default=render_cluster.ANSWERS)
    parser.add_argument(
        "--data",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Override one answer, YAML-parsed. Reaches the mixed "
        "optional-module arms the shipped all-on and all-off fixtures never render.",
    )
    parser.add_argument("--lib-path", type=Path, help="weisssrv-lib checkout for the collection.")
    parser.add_argument(
        "--allow-ref-mismatch",
        action="store_true",
        help="Accept a --lib-path checkout that is not at the pinned lib_ref, "
        "which is how an unreleased collection is exercised.",
    )
    parser.add_argument(
        "--self-checks",
        action="store_true",
        help="Also run the arms that read no render — this repository's own "
        "pipeline includes and vendored copies. Pass it on ONE invocation.",
    )
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
    # Checks run with a cwd of their own, so a relative path handed in here
    # would resolve against the wrong directory.
    for name in ("render_dir", "answers", "lib_path"):
        value = getattr(args, name)
        if value:
            setattr(args, name, value.resolve())
    # copier is a module, not a PATH binary, so _need() cannot see it. Without
    # this the render raises CalledProcessError instead of reporting.
    if not args.render_dir and _run([sys.executable, "-m", "copier", "--version"]).returncode:
        print("error: copier is not installed", file=sys.stderr)
        return 1
    workdir = Path(tempfile.mkdtemp(prefix="validate-render-"))
    try:
        overrides = {}
        for item in args.data:
            if "=" not in item:
                parser.error(f"--data expects NAME=VALUE, got {item!r}")
            name, _, raw = item.partition("=")
            overrides[name.strip()] = yaml.safe_load(raw)
        if overrides and args.render_dir:
            parser.error("--data has nothing to override when --render-dir supplies the render")
        if overrides:
            answers = {**yaml.safe_load(args.answers.read_text()), **overrides}
            args.answers = workdir / f"answers-{args.answers.stem}-overridden.yml"
            args.answers.write_text(yaml.safe_dump(answers, sort_keys=False))
            print(f"overriding {', '.join(sorted(overrides))}")
        render = args.render_dir or render_cluster.render(workdir, answers=args.answers)
        print(f"validating {render}")
        failed = []
        # Gates that claim byte-identity at the pin need this verdict, so it is
        # reached before any of them runs.
        ref_verified = False
        if args.lib_path:
            problems = lib_checkout_problems(args.lib_path, _pinned_lib_ref())
            if problems and not args.allow_ref_mismatch:
                print("\nFAILED:\n[lib-checkout] " + "\n".join(problems), file=sys.stderr)
                return 1
            for problem in problems:
                print(f"  warning: {problem}")
            ref_verified = not problems
        skipped_for_lib = []
        for name, fn, needs in CHECKS:
            if name in skip:
                print(f"  {name} skipped")
                continue
            if "lib" in needs and not args.lib_path:
                print(f"  {name} skipped (no --lib-path)")
                skipped_for_lib.append(name)
                continue
            try:
                fn(
                    render,
                    lib_path=args.lib_path,
                    workdir=workdir,
                    self_checks=args.self_checks,
                    ref_verified=ref_verified,
                )
            except Failure as exc:
                failed.append(f"[{name}] {exc}")
        if failed:
            print("\nFAILED:\n" + "\n\n".join(failed), file=sys.stderr)
            return 1
        # An unrequested lib skip is red: a third of the registry reporting
        # success is how ungated vendored drift reaches a generated cluster.
        if skipped_for_lib:
            print(
                "\nFAILED: " + ", ".join(skipped_for_lib) + " need --lib-path and did "
                "not run — pass --lib-path, or name them in --skip to accept the gap",
                file=sys.stderr,
            )
            return 1
        note = f" ({len(skip)} checks skipped: {', '.join(sorted(skip))})" if skip else ""
        print("render validated" + note)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
