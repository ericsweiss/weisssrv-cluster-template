"""Shared helpers for the suites under tests/: scripts, manifests, missing tools
and modules, playbook expressions through Ansible's templar, the pinned
collection's role defaults and durations."""

from __future__ import annotations

import functools
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# Where `task lib:sync` puts the weisssrv-lib checkout (Taskfile.yml's LIB_DIR),
# and the gate whose presence identifies one.
LOCAL_CHECKOUT = ".weisssrv-lib"
GATE_RELPATH = "scripts/check-vendored-copies.py"
# Reads library content at this commit-ish instead of the declared ref, for the
# window before the declared tag is cut.
REF_OVERRIDE_ENV = "WEISSSRV_LIB_REF_OVERRIDE"


def _lib_root() -> Path:
    """The weisssrv-lib checkout the vendored-copy gates read. A set
    $WEISSSRV_LIB_PATH is the only candidate; the local and sibling checkouts
    are tried only when it is unset."""
    explicit = os.environ.get("WEISSSRV_LIB_PATH")
    if explicit:
        assert (Path(explicit) / GATE_RELPATH).is_file(), (
            f"WEISSSRV_LIB_PATH={explicit} holds no {GATE_RELPATH}"
        )
        return Path(explicit)
    for candidate in (REPO / LOCAL_CHECKOUT, REPO.parent / "weisssrv-lib"):
        if (candidate / GATE_RELPATH).is_file():
            return candidate
    raise AssertionError(
        f"no weisssrv-lib checkout with {GATE_RELPATH} found — run `task lib:sync` "
        f"(it clones one into {LOCAL_CHECKOUT}/ at the pinned ref) or set "
        "$WEISSSRV_LIB_PATH. This gate never skips: an ungated vendored copy is "
        "exactly the drift it exists to catch."
    )


class CILoader(yaml.SafeLoader):
    """SafeLoader tolerating GitLab's `!reference` tags, subclassed so the
    constructor is not registered on the global SafeLoader."""


CILoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def pinned_lib_ref() -> str:
    """The one weisssrv-lib pin every library include and vendored copy reads.

    `variables.WEISSSRV_LIB_REF` is NOT it: that holds the truthy
    interpolation, so a reader of it compares copies against any checkout.
    """
    header = next(
        (
            doc
            for doc in yaml.load_all((REPO / ".gitlab-ci.yml").read_text(), Loader=CILoader)
            if isinstance(doc, dict) and "spec" in doc
        ),
        {},
    )
    entry = ((header.get("spec") or {}).get("inputs") or {}).get("lib_ref") or {}
    ref = entry.get("default")
    assert ref, ".gitlab-ci.yml spec.inputs.lib_ref.default is the single source of the pin"
    return str(ref)


def _git(lib: Path, *args: str) -> tuple[int, str]:
    """The exit code and stdout of one git command run in `lib`."""
    result = subprocess.run(
        ["git", "-C", str(lib), *args], capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout


def require_ref(lib: Path, ref: str) -> str:
    """The commit-ish to read library content at, once it resolves in `lib`.

    There is no working-tree fallback, which would certify the copies against an
    unreleased library state. $WEISSSRV_LIB_REF_OVERRIDE reads another commit-ish.
    """
    wanted = os.environ.get(REF_OVERRIDE_ENV) or ref
    code, _ = _git(lib, "rev-parse", "--verify", "--quiet", f"{wanted}^{{commit}}")
    assert code == 0, (
        f"{lib} has no {wanted} (fetch its tags, or `task lib:sync`), and this "
        f"gate never compares against a working tree. Set ${REF_OVERRIDE_ENV} to "
        "the commit the copies came from, or declare a ref that exists."
    )
    return wanted


def lib_file(relpath: str, ref: str) -> str:
    """A library file's text at `ref`, read through require_ref. Shared by the
    suites that read a library include's input defaults at the pin."""
    lib = _lib_root()
    wanted = require_ref(lib, ref)
    code, blob = _git(lib, "show", f"{wanted}:{relpath}")
    assert code == 0, f"{relpath} is absent at {wanted} in {lib}"
    return blob


def load_script(name: str | Path):
    """Import a hyphenated script under a module name Python accepts.

    A bare basename resolves under scripts/; a path is taken as given, for the
    gates that ship inside a manifest rather than in scripts/.
    """
    path = Path(name)
    path = path if path.is_absolute() else SCRIPTS / path
    spec = importlib.util.spec_from_file_location(
        path.name.replace("-", "_").removesuffix(".py"), path
    )
    assert spec and spec.loader, f"{path} is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_tool(name: str, gate_name: str, install_hint: str = "") -> None:
    """Stop a binary-driven gate that has no binary instead of skipping in CI.

    Skipping suits a workstation without the tool; under $CI the job never
    installed it, so the gate would certify a check it never ran.
    """
    if shutil.which(name):
        return
    if os.environ.get("CI"):
        pytest.fail(
            f"{name} is not on PATH, so the {gate_name} gate cannot run. "
            + (install_hint or "Install it in the job.")
        )
    pytest.skip(f"{name} is not on PATH")


def reported(path: Path) -> Path:
    return path.relative_to(REPO) if path.is_relative_to(REPO) else path


# Both YAML spellings reach the cluster, so both are walked: a `.yml` manifest
# dropped from the corpus takes every invariant declared in it with it.
K8S_SUFFIXES = ("*.yaml", "*.yml")


def k8s_documents(
    root: Path, *, suffix: str | None = None
) -> tuple[list[tuple[Path, dict]], list[str]]:
    """(path, document) for every mapping document under `root`, plus the files
    that would not parse, because a dropped file shrinks the caller's corpus
    silently. `suffix` narrows the walk to one glob."""
    found: list[tuple[Path, dict]] = []
    unreadable: list[str] = []
    globs = (suffix,) if suffix else K8S_SUFFIXES
    for path in sorted({p for glob in globs for p in root.rglob(glob)}):
        if not path.is_file():
            continue
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except (OSError, yaml.YAMLError) as exc:
            unreadable.append(f"{reported(path)}: {exc}")
            continue
        found += [(path, doc) for doc in docs if isinstance(doc, dict)]
    return found, unreadable


def assert_all_parsed(unreadable: list[str], kind: str) -> None:
    """The shared every-manifest-parsed assertion, named for what went unchecked."""
    assert not unreadable, (
        f"unparseable manifests — any {kind} they declare went unchecked:\n  "
        + "\n  ".join(unreadable)
    )


# cluster-config key -> (group_vars file, inventory variable) it mirrors, read
# from the literal gate's own table so the two can never disagree. Shared by the
# mirror gate and the restatement gate.
CONFIGMAP_INVENTORY_MIRROR = {
    key: (Path(path).name, var)
    for key, (path, var) in load_script("check-cluster-literals.py").INVENTORY_MIRRORS.items()
}

OBSERVABILITY = REPO / "kubernetes" / "infrastructure" / "observability"
APPS = REPO / "kubernetes" / "apps"


def release_alert_rules() -> tuple[list[tuple[str, dict]], list[str]]:
    """The alerts inside the kube-prometheus-stack HelmRelease's values.

    The release path is absolute: the extractor's default is relative to the
    working directory, so a caller running from elsewhere would extract nothing.
    """
    extract = load_script("extract-prometheus-config.py")
    release = REPO / extract.DEFAULT_RELEASE
    with tempfile.TemporaryDirectory() as scratch:
        rules_file = Path(scratch) / "rules.yaml"
        if extract.extract_rules(rules_file, release) != 0:
            return [], [f"{reported(release)}: no rule groups extracted"]
        doc = yaml.safe_load(rules_file.read_text()) or {}
    return [
        (rule["alert"], rule)
        for group in doc.get("groups") or []
        for rule in group.get("rules") or []
        if rule.get("alert")
    ], []


def prometheusrule_alert_rules() -> tuple[list[tuple[str, dict]], list[str]]:
    """The alerts any PrometheusRule CR declares, platform or app."""
    found: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    for tree in (OBSERVABILITY, APPS):
        if not tree.is_dir():
            continue
        docs, bad = k8s_documents(tree)
        unreadable += bad
        for _, doc in docs:
            if doc.get("kind") != "PrometheusRule":
                continue
            for group in (doc.get("spec") or {}).get("groups") or []:
                for rule in group.get("rules") or []:
                    if rule.get("alert"):
                        found.append((rule["alert"], rule))
    return found, unreadable


def loki_alert_rules(loki: Path | None = None) -> tuple[list[tuple[str, dict]], list[str]]:
    """The alerts the Loki ruler evaluates. These are bare `groups:` files the
    loki-sc-rules sidecar loads, not PrometheusRule CRs."""
    found: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    loki = loki or OBSERVABILITY / "loki"
    kustomization = loki / "kustomization.yaml"
    if not kustomization.is_file():
        return found, unreadable
    try:
        config = yaml.safe_load(kustomization.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return found, [f"{reported(kustomization)}: {exc}"]
    names = [
        name
        for entry in config.get("configMapGenerator") or []
        for name in entry.get("files") or []
    ]
    for name in names:
        # A `files:` entry may be written `key=path`; the path is what ships.
        path = loki / name.rsplit("=", 1)[-1]
        if not path.is_file():
            unreadable.append(
                f"{reported(path)}: named by {reported(kustomization)}'s "
                "configMapGenerator but not shipped, so its alerts went unchecked"
            )
            continue
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            unreadable.append(f"{reported(path)}: {exc}")
            continue
        for group in doc.get("groups") or []:
            for rule in group.get("rules") or []:
                if rule.get("alert"):
                    found.append((rule["alert"], rule))
    return found, unreadable


def alert_rules() -> tuple[list[tuple[str, dict]], list[str]]:
    """(alertname, rule) across the whole shipped rule corpus, plus the rule
    files that would not parse. A dropped file silently shrinks the corpus."""
    found: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    for walker in (release_alert_rules, prometheusrule_alert_rules, loki_alert_rules):
        walked, failed = walker()
        found += walked
        unreadable += failed
    return found, unreadable


VENDORED_MANIFEST = SCRIPTS / "vendored-manifest.yml"


def vendored_consumer_paths(sections: tuple[str, ...] = ("vendored", "forked")) -> list[str]:
    """Every consumer path scripts/vendored-manifest.yml registers in `sections`.

    A bare string is its own path; a mapping's `consumer` defaults to its `lib`,
    as the library engine reads it. `forked:` copies count by default."""
    manifest = yaml.safe_load(VENDORED_MANIFEST.read_text(encoding="utf-8")) or {}
    found = []
    for section in sections:
        for entry in manifest.get(section) or []:
            path = entry if isinstance(entry, str) else (entry.get("consumer") or entry.get("lib"))
            assert isinstance(path, str), f"vendored-manifest.yml entry names no path: {entry!r}"
            found.append(path)
    return found


def require_module(name: str, gate_name: str, install_hint: str = ""):
    """Import a module a gate needs: fail under $CI when it is missing, skip locally.

    The importorskip counterpart of require_tool, so a lost pip pin reds the job.
    """
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        if os.environ.get("CI"):
            pytest.fail(
                f"{name} does not import ({exc}) — the {gate_name} gate cannot run. "
                + (install_hint or "Install it in the job image.")
            )
        pytest.skip(f"{name} does not import", allow_module_level=True)


ANSIBLE_PIN_HINT = "The python-tests job installs it through its ansible-core pip pin."


def ansible_templar(variables: dict, gate_name: str):
    """Ansible's own Templar over `variables`, so a test renders Jinja as a play does."""
    template = require_module("ansible.template", gate_name, ANSIBLE_PIN_HINT)
    dataloader = require_module("ansible.parsing.dataloader", gate_name, ANSIBLE_PIN_HINT)
    return template.Templar(loader=dataloader.DataLoader(), variables=variables)


def trust_as_template(value, gate_name: str):
    """`value` with every nested string marked a trusted template, as vars files load."""
    template = require_module("ansible.template", gate_name, ANSIBLE_PIN_HINT)
    if isinstance(value, str):
        return template.trust_as_template(value)
    if isinstance(value, list):
        return [trust_as_template(v, gate_name) for v in value]
    if isinstance(value, dict):
        return {k: trust_as_template(v, gate_name) for k, v in value.items()}
    return value


def helm_release(path: Path) -> dict:
    """The HelmRelease document in a manifest that may hold siblings."""
    assert path.is_file(), f"{reported(path)} is missing — this guard checked nothing"
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, dict) and doc.get("kind") == "HelmRelease":
            return doc
    raise AssertionError(f"{reported(path)} holds no HelmRelease")


def release_values(path: Path) -> dict:
    """The inline `spec.values` of the HelmRelease in `path`."""
    return (helm_release(path).get("spec") or {}).get("values") or {}


# --------------------------------------------------------------------------
# The pinned weisssrv.infra collection and its role defaults
# --------------------------------------------------------------------------

REQUIREMENTS = REPO / "ansible" / "requirements.yml"


def _lib_checkout() -> Path:
    """The library checkout, resolved as the vendored-copy gate resolves it.

    A second copy of that resolution drifts from it. The absence of a checkout
    is left to pinned_source: an installed collection answers the same question.
    """
    try:
        return _lib_root()
    except AssertionError:
        return Path(os.environ.get("WEISSSRV_LIB_PATH") or REPO / LOCAL_CHECKOUT)


LIB_CHECKOUT = _lib_checkout()


def lib_git(*args: str) -> tuple[int, str]:
    """The exit code and stdout of one git command run in the library checkout."""
    return _git(LIB_CHECKOUT, *args)


def pinned_collection_version() -> str:
    """The weisssrv.infra version ansible/requirements.yml installs."""
    requirements = yaml.safe_load(REQUIREMENTS.read_text()) or {}
    for entry in requirements.get("collections") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        if "weisssrv-lib" in name or name.endswith("weisssrv/infra") or name == "weisssrv.infra":
            return str(entry.get("version", "")).lstrip("v")
    raise AssertionError("ansible/requirements.yml no longer pins weisssrv.infra")


def _by_variable(per_role: dict[str, dict]) -> dict[str, list[tuple[str, object]]]:
    """{role: defaults} turned into {variable: [(role, default), ...]}."""
    found: dict[str, list[tuple[str, object]]] = {}
    for role, defaults in sorted(per_role.items()):
        for key, value in defaults.items():
            found.setdefault(key, []).append((role, value))
    return found


def disk_role_defaults(roles: Path) -> dict[str, list[tuple[str, object]]]:
    """Role defaults from a roles tree on disk, in the flip gate's source order:
    defaults/main/ files merged in path order, then main.yml, then main.yaml."""
    gate = load_script("check-role-default-flips.py")
    per_role: dict[str, dict] = {}
    for role in sorted(path for path in roles.iterdir() if path.is_dir()):
        split = sorted(
            path for path in (role / gate.DEFAULTS_DIR).rglob("*")
            if path.is_file() and path.name.endswith(gate.YAML_SUFFIXES)
        )
        single = [role / name for name in gate.DEFAULTS_FILES if (role / name).is_file()]
        files = split or single[:1]
        if not files:
            continue
        merged: dict = {}
        for path in files:
            merged.update(yaml.safe_load(path.read_text()) or {})
        per_role[role.name] = merged
    found = _by_variable(per_role)
    assert found, f"parsed no role defaults out of {roles}"
    return found


@functools.lru_cache(maxsize=1)
def pinned_source() -> tuple[str, str]:
    """Where the pinned collection is readable: ('disk', roles path) or ('git', tag).
    An installed copy at that version wins, then the checkout at the matching tag;
    neither fails under $CI and skips locally, like require_module."""
    pinned = pinned_collection_version()
    for candidate in (
        # The three places this repo's own installs land: ansible-lint's
        # auto-install, a root-relative galaxy install, then the galaxy default
        # that `task ansible:install-collections` writes to.
        REPO / ".ansible-home/collections/ansible_collections/weisssrv/infra",
        REPO / "ansible_collections/weisssrv/infra",
        Path.home() / ".ansible/collections/ansible_collections/weisssrv/infra",
    ):
        manifest = candidate / "MANIFEST.json"
        if not manifest.is_file():
            continue
        match = re.search(r'"version"\s*:\s*"([^"]+)"', manifest.read_text())
        if match and match.group(1) == pinned and (candidate / "roles").is_dir():
            return "disk", str(candidate / "roles")

    ref = pinned if pinned.startswith("v") else f"v{pinned}"
    if not (LIB_CHECKOUT / ".git").exists():
        message = (
            f"no weisssrv.infra {pinned} installed and no library checkout at "
            f"{LIB_CHECKOUT} — run `task ansible:install-collections`, or "
            "`task lib:sync`, or set $WEISSSRV_LIB_PATH"
        )
        if os.environ.get("CI"):
            pytest.fail(f"{message}. The role-defaults gates cannot run.")
        pytest.skip(message)
    return "git", require_ref(LIB_CHECKOUT, ref)


def role_defaults() -> dict[str, list[tuple[str, object]]]:
    """{variable: [(role, default), ...]} for the pinned collection."""
    kind, source = pinned_source()
    if kind == "disk":
        return disk_role_defaults(Path(source))
    gate = load_script("check-role-default-flips.py")
    return _by_variable(gate.role_defaults(LIB_CHECKOUT, source, gate.COLLECTION))


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_DURATION_UNITS = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}


def to_seconds(value: str) -> float:
    """Parse a Go duration ('15m', '90s', '1h30m', '500ms') into seconds. Go has
    no day unit, so neither does this."""
    text = str(value)
    assert re.fullmatch(rf"(?:{_DURATION_PART.pattern})+", text), f"unparseable duration {value!r}"
    total = sum(float(n) * _DURATION_UNITS[unit] for n, unit in _DURATION_PART.findall(text))
    assert total > 0, f"unparseable duration {value!r}"
    return int(total) if total == int(total) else total


def cel(text: object) -> str:
    """A CEL expression with its whitespace collapsed, so a policy suite compares
    the expression a manifest carries rather than its line wrapping."""
    return " ".join(str(text).split())


TENANT_LABEL = "fluxcd.io/tenant"
TENANT_MATCH = {
    "namespaceSelector": {"matchExpressions": [{"key": TENANT_LABEL, "operator": "Exists"}]}
}


def _mappings(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]


def policy_spec(path: Path, kind: str, name: str) -> dict:
    """The spec of the one `kind` document named `name` in `path`."""
    found = [
        d for d in _mappings(path)
        if d.get("kind") == kind and (d.get("metadata") or {}).get("name") == name
    ]
    assert len(found) == 1, f"{reported(path)}: expected one {kind} {name}, found {len(found)}"
    return found[0]["spec"]


def policy_bindings(path: Path, name: str) -> list[dict]:
    """The spec of every ValidatingAdmissionPolicyBinding in `path` that binds `name`."""
    return [
        d["spec"] for d in _mappings(path)
        if d.get("kind") == "ValidatingAdmissionPolicyBinding"
        and (d.get("spec") or {}).get("policyName") == name
    ]


def assert_tenant_deny_binding(path: Path, name: str) -> None:
    """`name` has exactly one binding in `path`, and it denies on every tenant namespace."""
    assert policy_bindings(path, name) == [
        {"policyName": name, "validationActions": ["Deny"], "matchResources": TENANT_MATCH}
    ]


ANSWERS_FILE = REPO / ".copier-answers.yml"


def copier_answers() -> dict:
    """The answers this cluster was rendered with; a render always carries them."""
    assert ANSWERS_FILE.is_file(), ".copier-answers.yml is missing — a render always carries it"
    return yaml.safe_load(ANSWERS_FILE.read_text()) or {}
