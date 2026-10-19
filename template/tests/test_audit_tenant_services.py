"""audit-tenant-services.sh reports tenant Services and endpoints that predate
the admission policies. kubectl is a PATH stub answering from a fixture, so
each case pins how the script reads `kubectl get -o json` output."""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import REPO, require_tool

SCRIPT = REPO / "scripts" / "audit-tenant-services.sh"
CONTROLLER = "endpointslice-controller.k8s.io"

STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["AUDIT_FIXTURE"]) as fh:
        data = json.load(fh)
    if data.get("fail") and data["fail"] in args:
        sys.exit("stub kubectl: refused")
    ns = None
    if args[:1] == ["-n"]:
        ns, args = args[1], args[2:]
    assert args[0] == "get" and args[-2:] == ["-o", "json"], args
    kind = args[1].split(".")[0]
    if kind == "namespaces":
        assert args[2:4] == ["-l", "fluxcd.io/tenant"], args
        items = [{"metadata": {"name": n}} for n in data["tenants"]]
    else:
        items = data["objects"].get(ns, {}).get(kind, [])
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
    """
)


def service(name: str, selector: dict | None = None, kind: str = "ClusterIP") -> dict:
    spec: dict = {"type": kind}
    if selector is not None:
        spec["selector"] = selector
    return {"metadata": {"name": name}, "spec": spec}


def slice_(
    name: str,
    managed_by: str | None = CONTROLLER,
    service_name: str | None = "web",
    addresses: tuple[str, ...] = ("10.42.0.5",),
) -> dict:
    labels = {} if managed_by is None else {"endpointslice.kubernetes.io/managed-by": managed_by}
    if service_name is not None:
        labels["kubernetes.io/service-name"] = service_name
    return {
        "metadata": {"name": name, "labels": labels},
        "addressType": "IPv4",
        "endpoints": [{"addresses": [a]} for a in addresses],
    }


def endpoints(name: str, addresses: tuple[str, ...] = ()) -> dict:
    subsets = [{"addresses": [{"ip": a} for a in addresses]}] if addresses else []
    return {"metadata": {"name": name}, "subsets": subsets}


def pod(name: str, labels: dict, ip: str, phase: str = "Running") -> dict:
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"phase": phase, "podIP": ip, "podIPs": [{"ip": ip}]},
    }


def run(tmp_path: Path, fixture: dict) -> subprocess.CompletedProcess:
    require_tool("jq", "audit-tenant-services")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "kubectl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    (tmp_path / "fixture.json").write_text(json.dumps(fixture))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AUDIT_FIXTURE": str(tmp_path / "fixture.json"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


CLEAN_APP = {
    "services": [service("web", {"app": "web"})],
    "endpointslices": [slice_("web-abc12")],
    "endpoints": [endpoints("web", ("10.42.0.5",))],
    "pods": [pod("web-0", {"app": "web", "pod-template-hash": "x"}, "10.42.0.5")],
}


def test_a_clean_tenant_passes(tmp_path):
    result = run(tmp_path, {"tenants": ["demo"], "objects": {"demo": CLEAN_APP}})
    assert result.returncode == 0, result.stderr
    assert "1 tenant namespace" in result.stdout


def test_no_tenant_namespace_passes_and_says_so(tmp_path):
    result = run(tmp_path, {"tenants": [], "objects": {}})
    assert result.returncode == 0, result.stderr
    assert "no fluxcd.io/tenant namespaces" in result.stdout


@pytest.mark.parametrize(
    ("objects", "finding"),
    [
        (
            {"services": [service("ext", {"app": "x"}, kind="ExternalName")]},
            "demo Service/ext: type ExternalName",
        ),
        ({"services": [service("bare")]}, "demo Service/bare: no selector"),
        ({"services": [service("empty", {})]}, "demo Service/empty: no selector"),
        (
            {"endpointslices": [slice_("hand", "someone")]},
            "demo EndpointSlice/hand: managed-by someone",
        ),
        (
            {"endpointslices": [slice_("hand", None)]},
            "demo EndpointSlice/hand: managed-by unset",
        ),
        (
            {"endpointslices": [slice_("bare-x", "endpointslicemirroring-controller.k8s.io")]},
            "demo EndpointSlice/bare-x: managed-by endpointslicemirroring-controller.k8s.io",
        ),
        (
            {"endpointslices": [slice_("evil", addresses=("192.168.1.10",))]},
            "demo EndpointSlice/evil: address 192.168.1.10 is not a Pod selected by Service/web",
        ),
        (
            {"endpointslices": [slice_("nameless", service_name=None)]},
            "demo EndpointSlice/nameless: no kubernetes.io/service-name label",
        ),
        (
            {"endpointslices": [slice_("stray", service_name="ghost")]},
            "demo EndpointSlice/stray: no Service ghost with a selector",
        ),
        (
            {"endpoints": [endpoints("web", ("192.168.1.10",))]},
            "demo Endpoints/web: address 192.168.1.10 is not a Pod selected by Service/web",
        ),
        (
            {
                "pods": [pod("other", {"app": "other"}, "10.42.0.9")],
                "endpointslices": [slice_("sideways", addresses=("10.42.0.9",))],
            },
            "demo EndpointSlice/sideways: address 10.42.0.9 is not a Pod selected by Service/web",
        ),
        (
            {
                "pods": [pod("done", {"app": "web"}, "10.42.0.7", phase="Succeeded")],
                "endpointslices": [slice_("stale", addresses=("10.42.0.7",))],
            },
            "demo EndpointSlice/stale: address 10.42.0.7 is not a Pod selected by Service/web",
        ),
        (
            {"endpoints": [endpoints("orphan")]},
            "demo Endpoints/orphan: no Service with a selector",
        ),
        (
            {"services": [service("bare")], "endpoints": [endpoints("bare")]},
            "demo Endpoints/bare: no Service with a selector",
        ),
    ],
    ids=[
        "externalname",
        "selectorless",
        "empty-selector",
        "foreign-slice",
        "unlabelled-slice",
        "mirrored-slice",
        "labelled-but-foreign-slice",
        "slice-without-service-name",
        "slice-for-missing-service",
        "foreign-endpoints-address",
        "slice-to-unselected-pod",
        "slice-to-finished-pod",
        "orphan-endpoints",
        "selectorless-endpoints",
    ],
)
def test_each_pre_policy_object_is_reported(tmp_path, objects, finding):
    merged = {k: CLEAN_APP[k] + objects.get(k, []) for k in CLEAN_APP}
    result = run(tmp_path, {"tenants": ["demo"], "objects": {"demo": merged}})
    assert result.returncode == 1, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert finding in lines
    assert not [line for line in lines if line != finding and "web" in line.replace("web-", "")]


def test_a_controller_slice_whose_addresses_are_selected_pods_passes(tmp_path):
    objects = {
        "services": [service("web", {"app": "web"})],
        "endpointslices": [slice_("web-abc12", addresses=("10.42.0.5", "10.42.1.6"))],
        "endpoints": [endpoints("web", ("10.42.0.5", "10.42.1.6"))],
        "pods": [
            pod("web-0", {"app": "web"}, "10.42.0.5"),
            pod("web-1", {"app": "web", "extra": "y"}, "10.42.1.6"),
        ],
    }
    result = run(tmp_path, {"tenants": ["demo"], "objects": {"demo": objects}})
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_tenant_namespace_is_audited(tmp_path):
    result = run(
        tmp_path,
        {
            "tenants": ["demo", "other"],
            "objects": {"demo": CLEAN_APP, "other": {"services": [service("bare")]}},
        },
    )
    assert result.returncode == 1
    assert "other Service/bare: no selector" in result.stdout.splitlines()


def test_a_kubectl_failure_is_an_error_not_a_pass(tmp_path):
    result = run(
        tmp_path,
        {"tenants": ["demo"], "objects": {"demo": CLEAN_APP}, "fail": "endpointslices.discovery.k8s.io"},
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "OK" not in result.stdout
