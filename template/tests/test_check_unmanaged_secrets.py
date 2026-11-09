"""Tests for scripts/check-unmanaged-secrets.py."""
from __future__ import annotations

import io
import json
import re

import pytest
from conftest import REPO, load_script

mod = load_script("check-unmanaged-secrets.py")

FLUX_TASKFILE = REPO / "taskfiles" / "flux.yml"
# The bring-up spelling that creates a Secret no controller will ever own.
CREATE_SECRET = re.compile(
    r"kubectl\s+-n\s+(\S+)\s+create\s+secret\s+\w+\s+(\S+)"
)


def _secret(name="s", ns="apps", **meta) -> dict:
    out = {"metadata": {"name": name, "namespace": ns, **meta}, "data": {"k": "dg=="}}
    if "type" in meta:
        out["type"] = out["metadata"].pop("type")
    return out


def _run(secrets: list[dict], monkeypatch) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"items": secrets})))
    return mod.main()


def test_an_empty_secret_list_is_an_operator_error(monkeypatch, capsys):
    """A context or RBAC scope that returns nothing must not read as clean."""
    assert _run([], monkeypatch) == 2
    out = capsys.readouterr()
    assert "no Secrets" in out.err
    assert "OK" not in out.out


def test_non_json_stdin_is_an_operator_error(monkeypatch, capsys):
    """Exit 2 is reserved for an uninspectable subject; 1 means a real finding."""
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert mod.main() == 2
    assert "cannot parse stdin as JSON" in capsys.readouterr().err


def test_an_empty_items_payload_is_an_operator_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"items": []}'))
    assert mod.main() == 2
    assert "a gate that checks nothing is not a gate" in capsys.readouterr().err


def test_a_payload_that_is_not_a_list_is_an_operator_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"items": {"s": {}}}'))
    assert mod.main() == 2
    assert "not a Secret list" in capsys.readouterr().err


def test_hand_applied_secret_is_flagged(monkeypatch):
    hand_applied = _secret(
        annotations={"kubectl.kubernetes.io/last-applied-configuration": "{}"}
    )
    assert _run([hand_applied], monkeypatch) == 1


def test_owner_reference_counts_as_managed(monkeypatch):
    """ESO with creationPolicy: Owner sets an ownerReference on the Secret."""
    eso = _secret(ownerReferences=[{"kind": "ExternalSecret", "name": "app-secrets"}])
    assert _run([eso], monkeypatch) == 0


def test_helm_release_storage_is_managed(monkeypatch):
    assert _run([_secret(name="sh.helm.release.v1.app.v1", type="helm.sh/release.v1")],
                monkeypatch) == 0


def test_an_allowlisted_secret_passes(monkeypatch):
    monkeypatch.setitem(mod.ALLOWLIST, "apps/hand-made", "out-of-band on purpose")
    boot = _secret(name="hand-made", ns="apps")
    assert _run([boot], monkeypatch) == 0


def test_the_allowlist_is_exact_not_a_prefix(monkeypatch):
    """A near-miss name in the same namespace must still be flagged."""
    monkeypatch.setitem(mod.ALLOWLIST, "apps/hand-made", "out-of-band on purpose")
    impostor = _secret(name="hand-made-old", ns="apps")
    assert _run([impostor], monkeypatch) == 1


def _bootstrap_secrets() -> set[str]:
    """Every "namespace/name" the Flux bring-up tasks create by hand."""
    text = FLUX_TASKFILE.read_text(encoding="utf-8")
    return {f"{ns}/{name}" for ns, name in CREATE_SECRET.findall(text)}


def test_the_bring_up_secrets_are_credited():
    """A bootstrap Secret this repository creates itself has no controller to
    own it, so an uncredited one reds the gate on correct state. A secrets
    backend with no hand-made Secret credits none."""
    created = _bootstrap_secrets()
    assert created <= set(mod.ALLOWLIST), sorted(created - set(mod.ALLOWLIST))
    credited = {key for key, why in mod.ALLOWLIST.items() if why.startswith("ESO bootstrap")}
    assert credited <= created, (
        f"ALLOWLIST credits bootstrap Secrets {sorted(credited - created)} that no "
        f"`kubectl create secret` call in {FLUX_TASKFILE.name} makes"
    )


# Component directory in the render -> the Secret its controller mints with no
# ownership marker. A rendered component needs its entry; an absent one must
# not leave a stale entry behind.
CONTROLLER_MINTED = {
    "kubernetes/infrastructure/controllers/vpa": "vpa-system/vpa-tls-secret",
    "kubernetes/infrastructure/observability/kube-prometheus-stack": (
        "observability/kube-prometheus-stack-admission"
    ),
    "kubernetes/infrastructure/controllers/tailscale-operator": "tailscale/operator",
    "kubernetes/apps/gitlab-runner": "flux-system/gitlab-receiver-flux-system",
}


def minted_mismatches(root, allowlist) -> list[str]:
    problems = []
    for component, secret in sorted(CONTROLLER_MINTED.items()):
        rendered = (root / component).is_dir()
        if rendered and secret not in allowlist:
            problems.append(f"{component} renders but {secret} is not allowlisted")
        if not rendered and secret in allowlist:
            problems.append(f"{secret} is allowlisted but {component} is not rendered")
    return problems


def test_every_rendered_component_s_minted_secret_is_allowlisted():
    assert any((REPO / component).is_dir() for component in CONTROLLER_MINTED), (
        "no component this gate knows renders, so it compared nothing"
    )
    problems = minted_mismatches(REPO, mod.ALLOWLIST)
    assert not problems, "\n".join(problems)


def test_a_missing_minted_secret_entry_is_reported(tmp_path):
    """Mutation case: a rendered component without its entry, and an entry
    without its component, are both reported."""
    (tmp_path / "kubernetes/infrastructure/controllers/vpa").mkdir(parents=True)
    assert minted_mismatches(tmp_path, {}) == [
        "kubernetes/infrastructure/controllers/vpa renders but vpa-system/vpa-tls-secret "
        "is not allowlisted"
    ]
    assert minted_mismatches(tmp_path, {"vpa-system/vpa-tls-secret": "x", "tailscale/operator": "x"}) == [
        "tailscale/operator is allowlisted but "
        "kubernetes/infrastructure/controllers/tailscale-operator is not rendered"
    ]


def test_the_flux_bootstrap_deploy_key_is_credited():
    """`flux bootstrap` writes it directly, outside any Kustomization."""
    assert "flux-system/flux-system" in mod.ALLOWLIST


@pytest.mark.parametrize("entry", sorted(mod.ALLOWLIST))
def test_every_allowlist_entry_states_a_reason(entry):
    """An entry is a claim that the value rotates outside this repository, so a
    blank one credits nothing."""
    assert mod.ALLOWLIST[entry].strip()


def test_a_hand_created_service_account_token_is_flagged(monkeypatch):
    """A long-lived token with no owner is the credential this gate surfaces."""
    sa_token = _secret(name="sa-token", type="kubernetes.io/service-account-token")
    assert _run([sa_token], monkeypatch) == 1


def test_a_flux_applied_service_account_token_is_managed(monkeypatch):
    sa_token = _secret(
        name="sa-token",
        type="kubernetes.io/service-account-token",
        labels={"kustomize.toolkit.fluxcd.io/name": "apps"},
    )
    assert _run([sa_token], monkeypatch) == 0


# One marker per case, written where its manager writes it, so dropping any
# single key from the script's ownership sets reds exactly one case.
OWNERSHIP_MARKERS = {
    "kustomize.toolkit.fluxcd.io/name": {"labels": {"kustomize.toolkit.fluxcd.io/name": "apps"}},
    "helm.toolkit.fluxcd.io/name": {"labels": {"helm.toolkit.fluxcd.io/name": "app"}},
    "meta.helm.sh/release-name": {"annotations": {"meta.helm.sh/release-name": "app"}},
    "controller.cert-manager.io/fao": {"labels": {"controller.cert-manager.io/fao": "true"}},
    "cert-manager.io/certificate-name": {
        "annotations": {"cert-manager.io/certificate-name": "example-com"}
    },
    "tailscale.com/managed": {"labels": {"tailscale.com/managed": "true"}},
    "reconcile.external-secrets.io/": {
        "annotations": {"reconcile.external-secrets.io/data-hash": "abc"}
    },
    "listener.cattle.io/": {"annotations": {"listener.cattle.io/cn-192.0.2.1": "192.0.2.1"}},
    "app.kubernetes.io/managed-by=Helm": {"labels": {"app.kubernetes.io/managed-by": "Helm"}},
}


@pytest.mark.parametrize("marker", sorted(OWNERSHIP_MARKERS))
def test_each_ownership_marker_alone_is_managed(monkeypatch, marker):
    assert _run([_secret(**OWNERSHIP_MARKERS[marker])], monkeypatch) == 0


def test_every_ownership_key_has_a_positive_case():
    declared = set(mod.MANAGED_KEYS) | set(mod.MANAGED_KEY_PREFIXES)
    declared.add("=".join(mod.HELM_MANAGED_BY))
    assert declared == set(OWNERSHIP_MARKERS)


@pytest.mark.parametrize(
    "meta",
    [
        {"labels": {"app.kubernetes.io/managed-by": "kubectl"}},
        {"annotations": {"kustomize.toolkit.fluxcd.io/prune": "disabled"}},
        {"labels": {"kustomize.toolkit.fluxcd.io/name": ""}},
        {"annotations": {"meta.helm.sh/release-namespace": "apps"}},
    ],
    ids=["copied-managed-by", "hand-added-prune", "empty-flux-name", "helm-namespace-only"],
)
def test_a_self_asserted_marker_is_not_ownership(monkeypatch, meta):
    assert _run([_secret(**meta)], monkeypatch) == 1


def test_offender_message_lists_keys_not_values(monkeypatch, capsys):
    hand_applied = {
        "metadata": {"name": "mealie-secrets", "namespace": "recipes"},
        "data": {"openai-api-key": "c2VjcmV0", "smtp-password": "c2VjcmV0"},
    }
    assert _run([hand_applied], monkeypatch) == 1
    err = capsys.readouterr().err
    assert "openai-api-key" in err
    assert "c2VjcmV0" not in err


def test_the_failure_output_points_at_docs_this_repository_has(monkeypatch, capsys):
    hand_applied = _secret(
        annotations={"kubectl.kubernetes.io/last-applied-configuration": "{}"}
    )
    assert _run([hand_applied], monkeypatch) == 1
    err = capsys.readouterr().err
    cited = set(re.findall(r"docs/[\w./-]*\w", err))
    assert cited, "the failure output names no runbook"
    assert all((REPO / doc).is_file() for doc in cited), sorted(cited)
