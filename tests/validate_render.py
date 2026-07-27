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


def check_vendored(render: Path, lib_path: Path | None) -> None:
    """Every vendored script must be byte-identical to the library's copy.

    The template ships copies of weisssrv-lib's generic tooling so a generated
    cluster's CI never has to clone another repository. Nothing else notices
    when a copy drifts: the fix the library shipped is simply absent, and the
    next `task lib:sync` refresh silently reverts whatever was edited here.
    `flux-env.sh` is the one script written locally and has no library twin, so
    it is not compared — it must not be a fork of a vendored file either, which
    is why `flux-render.sh` is compared like the rest.
    """
    if not lib_path:
        raise Failure("--lib-path is required to check the vendored copies")
    lib_scripts = lib_path / "scripts"
    if not lib_scripts.is_dir():
        raise Failure(f"{lib_scripts} does not exist (is --lib-path a weisssrv-lib checkout?)")

    local = {"flux-env.sh"}
    site_data = {".yml", ".yaml", ".env", ".conf", ".toml"}
    vendored, drifted, orphaned = [], [], []
    for path in sorted((render / "scripts").iterdir()):
        if not path.is_file() or path.name in local or path.name == "README.md":
            continue
        if path.suffix in site_data or path.name == "version-registry.py":
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
            "vendored scripts differ from the library at the pinned ref "
            "(re-copy them and review the diff): " + ", ".join(drifted)
        )
    if orphaned:
        problems.append(
            "scripts/ holds files the library does not ship, and they are not "
            "declared local: " + ", ".join(orphaned)
        )
    if problems:
        raise Failure("\n".join(problems))
    print(f"  vendored ok ({len(vendored)} scripts byte-identical to the library)")


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
        "--skip", default="", help="Comma-separated: yamllint,flux,vendored,ansible"
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
        if "vendored" in skip or not args.lib_path:
            print("  vendored skipped" if "vendored" in skip else "  vendored skipped (no --lib-path)")
        else:
            try:
                check_vendored(render, args.lib_path)
            except Failure as exc:
                failed.append(f"[vendored] {exc}")
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
