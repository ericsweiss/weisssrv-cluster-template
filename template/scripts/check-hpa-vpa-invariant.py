#!/usr/bin/env python3
"""Assert no workload has both an HPA and a CPU-controlling VPA.

A HorizontalPodAutoscaler and a VerticalPodAutoscaler must never drive the same
resource on the same workload: the HPA scales replica count on (typically) CPU
utilization while the VPA updater evicts pods to resize CPU requests — they
fight, and pods thrash. The rule this enforces is that any workload with an HPA
carries a memory-only VPA (controlledResources: [memory], no cpu).

This guards that invariant in CI. It reads a stream of rendered Kubernetes
manifests on stdin (the corpus `task flux:lint` builds with
`kustomize build | envsubst` — no `helm template`), then for every HPA finds the
VPAs targeting the same (namespace, kind, name) and fails if any of them controls
a resource the HPA also scales. Because the corpus is kustomize-only, this covers
STANDALONE HPAs/VPAs in the kustomize-build stream.

Chart-native HPAs live inside HelmReleases and are NOT expanded into the
kustomize corpus, so the generic join above cannot see them. Their paired VPAs
ARE in the corpus, though, so with --require-chart-native-vpas (on the *full*
corpus) this also statically asserts each declared chart-native target has a
mutating (Auto/Initial) VPA that excludes cpu — an Off/recommend-only VPA does
not count, since it never actually right-sizes. The flag is off by default so
unit tests can exercise the generic join on minimal streams.

With --require-chart-native-vpas it ALSO asserts the "no CPU limits" policy: CPU
is compressible, so a CPU limit only adds CFS throttling that hurts latency and
inflates the CPU% a CPU-based HPA reads. The check covers both rendered pod specs
and HelmRelease `.spec.values`.

The same flag enforces the VPA memory-cap rule, scoped to what each policy
controls (`maxAllowed.memory` against the container's memory limit):

    policy shape                                    cap rule
    controlledValues: RequestsOnly (Auto/Initial)   == the limit is CORRECT
    limit-controlling (RequestsAndLimits or unset)  must stay BELOW the limit —
                                                    the updater rescales the
                                                    limit with the request, so a
                                                    cap at the limit leaves no
                                                    ceiling that binds
    updateMode/mode Off                             exempt from the == rule

    above the limit                                 always wrong, every shape

The chart-native target list, the CPU-limit allowlist and the VPA-cap allowlist
are consumer data, read from --policy-config (YAML/JSON, all keys optional):

    chart_native_hpa_targets:
      - {namespace: traefik, kind: Deployment, name: traefik, source: chart autoscaling}
    cpu_limit_allowlist:
      - kube-system/DaemonSet/foo   # rationale
    vpa_cap_allowlist:
      - apps/VerticalPodAutoscaler/foo   # rationale (cap not re-derived yet)

Limitation: a CPU limit baked into a third-party chart's subchart defaults that
is NOT overridden in `.spec.values` is invisible here (the corpus is kustomize-
only, no `helm template`). validate-helm-values.py renders the value-heavy
releases via `helm template` and reuses cpu_limit_violations to catch those. The
cap rule has the same blind spot from the other side: a VPA whose target workload
is rendered by a chart has no limit to compare against in this corpus, so it is
skipped rather than guessed at.

Usage (on the accumulated full corpus):
  kustomize build <path> | envsubst >> corpus
  check-hpa-vpa-invariant.py --require-chart-native-vpas \
      --policy-config autoscaling-policy.yaml < corpus
"""
from __future__ import annotations

import argparse
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

HPA_KIND = "HorizontalPodAutoscaler"
VPA_KIND = "VerticalPodAutoscaler"


class Policy:
    """The consumer data from --policy-config; see the module docstring.

    A value, not module state: validate-helm-values.py imports this module to
    load the same file, and accumulating into globals made a second load add to
    the first instead of replacing it. (A plain class, not a dataclass: this
    module is loaded by path with importlib, where @dataclass cannot resolve its
    own annotations.)
    """

    def __init__(
        self, chart_native_hpa_targets=None, cpu_limit_allowlist=None, vpa_cap_allowlist=None
    ) -> None:
        # (namespace, target-kind, target-name) -> where the chart-native HPA comes from.
        self.chart_native_hpa_targets: dict[tuple[str, str, str], str] = dict(
            chart_native_hpa_targets or {}
        )
        # "namespace/Kind/name" workloads intentionally permitted a CPU limit.
        self.cpu_limit_allowlist: set[str] = set(cpu_limit_allowlist or ())
        # "namespace/VerticalPodAutoscaler/name" policies whose memory cap has not
        # been re-derived against its limit yet. A grace list, not an exemption:
        # every entry carries a rationale in the consumer's policy file.
        self.vpa_cap_allowlist: set[str] = set(vpa_cap_allowlist or ())


def _target_key(ns: str, ref: dict) -> tuple[str, str, str]:
    """(namespace, target-kind, target-name) — the join key between HPA and VPA."""
    return (ns or "default", ref.get("kind", ""), ref.get("name", ""))


def _hpa_metrics(spec: dict) -> set[str]:
    """Resource names an HPA scales on (cpu/memory).

    An HPA with no `metrics` field (or an empty list) defaults to CPU 80% under
    autoscaling/v2, so that case yields {"cpu"}. But if `metrics` IS present and
    holds only non-Resource entries (External/Object/Pods), the HPA scales on
    nothing the VPA touches — return the empty set so no phantom CPU is assumed.
    """
    metrics = spec.get("metrics") or []
    if not metrics:
        return {"cpu"}
    resources: set[str] = set()
    for metric in metrics:
        mtype = metric.get("type")
        if mtype == "Resource":
            name = (metric.get("resource") or {}).get("name")
        elif mtype == "ContainerResource":
            # Per-container CPU/memory target — still a cpu/memory HPA.
            name = (metric.get("containerResource") or {}).get("name")
        else:
            continue
        if name:
            resources.add(str(name).lower())
    return resources


def _vpa_resources(spec: dict) -> set[str]:
    """Resources a VPA controls. Default (no controlledResources) is cpu+memory."""
    controlled: set[str] = set()
    policies = (spec.get("resourcePolicy") or {}).get("containerPolicies", []) or []
    if not policies:
        # No policy means the VPA controls everything by default.
        return {"cpu", "memory"}
    for p in policies:
        if (p.get("mode") or "").lower() == "off":
            # Per-container Off policy is recommend-only — not mutating.
            continue
        cr = p.get("controlledResources")
        if cr is None:
            controlled |= {"cpu", "memory"}
        else:
            controlled |= {str(r).lower() for r in cr}
    # containerPolicies scope by containerName: a policy naming one container
    # says nothing about the pod's other containers, which the VPA still
    # controls with the default (cpu+memory). Without a '*' catch-all policy,
    # fail closed and count those defaults so e.g. a named-container
    # memory-only policy cannot hide a real CPU/HPA clash on a sidecar.
    if not any(p.get("containerName") == "*" for p in policies):
        controlled |= {"cpu", "memory"}
    return controlled


# --- "no CPU limits" policy ---------------------------------------------------
POD_SPEC_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "Pod"}


def load_policy(path) -> Policy:
    """Read a --policy-config file and return it. Mutates nothing.

    SHARED: validate-helm-values.py imports this (and `cpu_limit_violations`)
    so the kustomize-side and helm-rendered-side checks honor one allowlist.
    """
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    policy = Policy()
    for entry in doc.get("chart_native_hpa_targets") or []:
        missing = [k for k in ("namespace", "kind", "name") if not entry.get(k)]
        if missing:
            raise ValueError(f"{path}: chart-native target {entry!r} is missing {missing}")
        policy.chart_native_hpa_targets[(entry["namespace"], entry["kind"], entry["name"])] = str(
            entry.get("source", "chart-native HPA")
        )
    for item in doc.get("cpu_limit_allowlist") or []:
        policy.cpu_limit_allowlist.add(str(item))
    for item in doc.get("vpa_cap_allowlist") or []:
        policy.vpa_cap_allowlist.add(str(item))
    return policy


def _containers_of(doc: dict) -> list[dict]:
    """All containers (init + regular + ephemeral) of a pod-spec workload, else []."""
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if kind == "Pod":
        pod = spec
    elif kind == "CronJob":
        pod = ((((spec.get("jobTemplate") or {}).get("spec") or {})
                .get("template") or {}).get("spec") or {})
    elif kind in POD_SPEC_KINDS:
        pod = (spec.get("template") or {}).get("spec") or {}
    else:
        return []
    out: list[dict] = []
    for key in ("initContainers", "containers", "ephemeralContainers"):
        v = pod.get(key)
        if isinstance(v, list):
            out.extend(c for c in v if isinstance(c, dict))
    return out


def _find_values_cpu_limits(node, path: str = "") -> list[str]:
    """Recursively find `limits.cpu` inside a HelmRelease `.spec.values` tree."""
    hits: list[str] = []
    if isinstance(node, dict):
        lim = node.get("limits")
        # `cpu: null`/`""` clears a chart default rather than setting a limit
        # (k8s treats it as "no CPU limit"), so don't flag a merely-present key.
        if isinstance(lim, dict) and lim.get("cpu") not in (None, ""):
            key = f"{path}.limits.cpu" if path else "limits.cpu"
            hits.append(f"{key}={lim.get('cpu')}")
        for k, v in node.items():
            if k != "limits":
                hits.extend(_find_values_cpu_limits(v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits.extend(_find_values_cpu_limits(v, f"{path}[{i}]"))
    return hits


def cpu_limit_violations(docs: list[dict], allowlist: set[str] | None = None) -> list[str]:
    """Flag any pod-spec container or HelmRelease values that set a CPU limit."""
    allowed = allowlist or set()
    out: list[str] = []
    for d in docs:
        kind = d.get("kind")
        meta = d.get("metadata") or {}
        wlkey = f"{meta.get('namespace', '')}/{kind}/{meta.get('name', '?')}"
        if wlkey in allowed:
            continue
        if kind == "HelmRelease":
            values = (d.get("spec") or {}).get("values") or {}
            for hit in _find_values_cpu_limits(values, "values"):
                out.append(f"  {wlkey}: HelmRelease sets a CPU limit ({hit})")
        else:
            for c in _containers_of(d):
                lim = (c.get("resources") or {}).get("limits") or {}
                if lim.get("cpu") not in (None, ""):
                    out.append(
                        f"  {wlkey}: container {c.get('name', '?')!r} sets "
                        f"limits.cpu={lim.get('cpu')}"
                    )
    return out


# --- VPA memory-cap rule ------------------------------------------------------
_BINARY_SUFFIXES = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL_SUFFIXES = {
    "n": 1e-9, "u": 1e-6, "m": 1e-3,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
}


def parse_quantity(value) -> float | None:
    """A Kubernetes quantity in bytes, or None when it cannot be read.

    Unparseable is None rather than 0: a value this cannot compare must not
    become a violation invented out of a parse failure.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        # Plain and decimal-exponent forms ("1", "1.5", "1e6") — tried first so
        # the exponent form is never mistaken for the "E" (exa) suffix.
        return float(text)
    except ValueError:
        pass
    for suffix, mult in _BINARY_SUFFIXES.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * mult
            except ValueError:
                return None
    mult = _DECIMAL_SUFFIXES.get(text[-1])
    if mult is None:
        return None
    try:
        return float(text[:-1]) * mult
    except ValueError:
        return None


def _workload_memory_limits(docs: list[dict]) -> dict[tuple[str, str, str], dict[str, str]]:
    """(namespace, kind, name) -> {container name: its memory limit}.

    A workload present with no limits still gets an (empty) entry: "visible in
    the corpus" and "has a limit to compare against" are different facts.
    """
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    for d in docs:
        kind = d.get("kind")
        if kind not in POD_SPEC_KINDS and kind != "CronJob":
            continue
        meta = d.get("metadata") or {}
        name = meta.get("name")
        if not name:
            continue
        limits: dict[str, str] = {}
        for c in _containers_of(d):
            mem = ((c.get("resources") or {}).get("limits") or {}).get("memory")
            if mem not in (None, ""):
                limits[str(c.get("name") or "?")] = str(mem)
        out[(meta.get("namespace") or "default", kind, str(name))] = limits
    return out


def vpa_cap_violations(docs: list[dict], allowlist: set[str] | None = None) -> list[str]:
    """Flag `maxAllowed.memory` caps that cannot do their job.

    Above the container's limit is always wrong — the updater can never apply a
    recommendation the kubelet would reject. Exactly AT the limit is wrong only
    where the policy also controls limits (`controlledValues: RequestsAndLimits`
    or unset, mode not Off): there the limit is rescaled with the request, so a
    cap at the limit leaves no ceiling that ever binds. Under `RequestsOnly` the
    same cap == limit is the correct shape — the limit is hand-set and only the
    request moves.

    Targets the kustomize-only corpus does not render (chart-native workloads)
    have no limit to compare against and are skipped.
    """
    allowed = allowlist or set()
    workloads = _workload_memory_limits(docs)
    out: list[str] = []
    for d in docs:
        if d.get("kind") != VPA_KIND:
            continue
        meta = d.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        vpakey = f"{ns}/{VPA_KIND}/{meta.get('name', '?')}"
        if vpakey in allowed:
            continue
        spec = d.get("spec") or {}
        ref = spec.get("targetRef") or {}
        if not ref.get("name"):
            continue
        target = _target_key(ns, ref)
        limits = workloads.get(target)
        if not limits:
            continue
        vpa_off = str((spec.get("updatePolicy") or {}).get("updateMode", "Auto")).lower() == "off"
        tns, tkind, tname = target
        policies = (spec.get("resourcePolicy") or {}).get("containerPolicies", []) or []
        # An exact-named containerPolicy overrides "*" for that container, so
        # the wildcard's cap must not be judged against explicitly-covered ones.
        explicit = {p.get("containerName") for p in policies
                    if p.get("containerName") not in (None, "*")}
        for p in policies:
            cap_raw = (p.get("maxAllowed") or {}).get("memory")
            cap = parse_quantity(cap_raw)
            if cap is None:
                continue
            off = vpa_off or str(p.get("mode") or "").lower() == "off"
            # Unset controlledValues defaults to RequestsAndLimits, i.e. the
            # policy moves the limit too.
            controls_limits = (
                str(p.get("controlledValues") or "RequestsAndLimits").lower() != "requestsonly"
            )
            cname = p.get("containerName")
            targets = (
                {k: v for k, v in limits.items() if k not in explicit}
                if cname in (None, "*")
                else {k: v for k, v in limits.items() if k == cname}
            )
            for container, limit_raw in sorted(targets.items()):
                limit = parse_quantity(limit_raw)
                if limit is None or limit <= 0:
                    continue
                if cap > limit * (1 + 1e-9):
                    out.append(
                        f"  {vpakey}: maxAllowed.memory {cap_raw} is above the {limit_raw} "
                        f"limit of container {container!r} in {tns}/{tkind}/{tname} — a "
                        f"recommendation the kubelet would reject; lower the cap or raise "
                        f"the limit in the same commit"
                    )
                elif controls_limits and not off and abs(cap - limit) <= limit * 1e-9:
                    out.append(
                        f"  {vpakey}: maxAllowed.memory {cap_raw} equals the limit of "
                        f"container {container!r} in {tns}/{tkind}/{tname} while the policy "
                        f"also controls limits (controlledValues: "
                        f"{p.get('controlledValues') or 'unset'}) — the updater rescales the "
                        f"limit with the request, so that ceiling never binds; cap below the "
                        f"limit, or set controlledValues: RequestsOnly with a hand-set limit"
                    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HPA/VPA + CPU-limit policy gate.")
    parser.add_argument("--require-chart-native-vpas", action="store_true")
    parser.add_argument("--policy-config", help="YAML/JSON policy data (see module docstring)")
    args = parser.parse_args(argv)
    policy = Policy()
    if args.policy_config:
        try:
            policy = load_policy(args.policy_config)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            sys.exit(f"Failed to load --policy-config: {exc}")

    # safe_load_all is lazy, so parse errors surface during iteration — wrap the
    # loop (not just the generator) so a malformed stream exits cleanly. Also
    # flatten `kind: List` and top-level YAML lists so wrapped resources count.
    docs: list[dict] = []
    try:
        for raw in yaml.safe_load_all(sys.stdin):
            if isinstance(raw, dict):
                if raw.get("kind") == "List" and isinstance(raw.get("items"), list):
                    docs.extend(i for i in raw["items"] if isinstance(i, dict))
                else:
                    docs.append(raw)
            elif isinstance(raw, list):
                docs.extend(i for i in raw if isinstance(i, dict))
    except yaml.YAMLError as exc:
        sys.exit(f"Failed to parse YAML input: {exc}")

    # Multiple HPAs or VPAs can target one workload, so aggregate per key rather
    # than last-wins: union the resource sets so a memory-only VPA can never mask
    # a cpu-controlling one on the same target.
    hpas: dict[tuple[str, str, str], set[str]] = {}
    vpas: dict[tuple[str, str, str], set[str]] = {}  # mutating VPAs only (Off skipped)
    vpa_names: dict[tuple[str, str, str], list[str]] = {}

    for d in docs:
        kind = d.get("kind")
        meta = d.get("metadata") or {}
        ns = meta.get("namespace", "")
        spec = d.get("spec") or {}
        if kind == HPA_KIND:
            ref = spec.get("scaleTargetRef") or {}
            if not ref.get("name"):
                continue
            key = _target_key(ns, ref)
            hpas[key] = hpas.get(key, set()) | _hpa_metrics(spec)
        elif kind == VPA_KIND:
            ref = spec.get("targetRef") or {}
            if not ref.get("name"):
                continue
            key = _target_key(ns, ref)
            # updateMode "Off" is recommend-only: it never mutates pods, so it
            # cannot fight an HPA (this is how coredns pairs a min==max HPA pin
            # with a right-sizing VPA). Only mutating modes can conflict.
            mode = (spec.get("updatePolicy") or {}).get("updateMode", "Auto")
            if str(mode).lower() == "off":
                continue
            vpas[key] = vpas.get(key, set()) | _vpa_resources(spec)
            vpa_names.setdefault(key, []).append(meta.get("name", "?"))

    violations: list[str] = []
    for key in sorted(hpas):
        hpa_res = hpas[key]
        if key not in vpas:
            continue
        vpa_res = vpas[key]
        # hpa_res already encodes the autoscaling/v2 default: it is {"cpu"} when
        # no metrics were declared and empty when metrics held only non-Resource
        # (External/Object/Pods) entries, which can't clash with a VPA.
        clash = hpa_res & vpa_res
        if clash:
            ns, tkind, tname = key
            names = ", ".join(repr(n) for n in sorted(vpa_names[key]))
            violations.append(
                f"  {ns}/{tkind}/{tname}: HPA scales {sorted(hpa_res)} "
                f"but VPA(s) {names} also control {sorted(clash)} "
                f"(set the VPA to controlledResources excluding {sorted(clash)})"
            )

    # Static check for chart-native HPAs (their HPA isn't in the corpus, but their
    # VPA is). Opt-in: only meaningful on the full rendered corpus flux:lint builds.
    if args.require_chart_native_vpas:
        for key, source in sorted(policy.chart_native_hpa_targets.items()):
            ns, tkind, tname = key
            # `not vpas.get(key)` (vs `key not in vpas`) also catches a mutating
            # VPA whose every containerPolicy is mode:Off — it registers with an
            # empty controlled set but right-sizes nothing, so it must not count.
            if not vpas.get(key):
                violations.append(
                    f"  {ns}/{tkind}/{tname}: chart-native HPA ({source}) has no "
                    f"mutating (Auto/Initial) VPA in the rendered corpus — add a "
                    f"memory-only VPA (controlledResources: [memory]) so CPU stays "
                    f"HPA-owned and memory is actually right-sized (an Off VPA "
                    f"recommends but never resizes, so it does not satisfy this)"
                )
            elif "cpu" in vpas.get(key, set()):
                names = ", ".join(repr(n) for n in sorted(vpa_names.get(key, [])))
                violations.append(
                    f"  {ns}/{tkind}/{tname}: chart-native HPA ({source}) scales cpu "
                    f"but mutating VPA(s) {names} also control cpu — set "
                    f"controlledResources to exclude cpu (memory-only)"
                )

    cpu_violations = (
        cpu_limit_violations(docs, policy.cpu_limit_allowlist)
        if args.require_chart_native_vpas else []
    )
    cap_violations = (
        vpa_cap_violations(docs, policy.vpa_cap_allowlist)
        if args.require_chart_native_vpas else []
    )

    failed = False
    if violations:
        print("HPA/VPA invariant violated — same resource driven by both:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        failed = True
    if cpu_violations:
        print(
            "CPU-limit policy violated — pods/HelmReleases must not set a CPU limit "
            "(compressible resource; CFS throttling hurts latency and distorts "
            "CPU-based HPAs). Offenders:",
            file=sys.stderr,
        )
        print("\n".join(cpu_violations), file=sys.stderr)
        failed = True
    if cap_violations:
        print(
            "VPA cap policy violated — maxAllowed.memory must stay under the container's "
            "memory limit, and may equal it only where the policy does not control limits "
            "(controlledValues: RequestsOnly). Offenders:",
            file=sys.stderr,
        )
        print("\n".join(cap_violations), file=sys.stderr)
        print(
            "  (a cap awaiting re-derivation goes in the policy file's vpa_cap_allowlist "
            "with its rationale, not left silently violating)",
            file=sys.stderr,
        )
        failed = True
    if failed:
        return 1

    print(
        f"HPA/VPA invariant OK ({len(hpas)} HPAs, {len(vpas)} VPAs checked"
        + (f", {len(policy.chart_native_hpa_targets)} chart-native targets asserted"
           ", CPU-limit policy OK, VPA cap policy OK"
           if args.require_chart_native_vpas else "")
        + ")"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
