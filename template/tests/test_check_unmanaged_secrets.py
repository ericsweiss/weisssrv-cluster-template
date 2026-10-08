"""Tests for scripts/check-unmanaged-secrets.py."""
from __future__ import annotations

import io
import json

from conftest import load_script

mod = load_script("check-unmanaged-secrets.py")


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


def test_helm_membership_annotation_is_managed(monkeypatch):
    assert _run([_secret(annotations={"meta.helm.sh/release-name": "app"})],
                monkeypatch) == 0


def test_flux_label_is_managed(monkeypatch):
    assert _run([_secret(labels={"kustomize.toolkit.fluxcd.io/name": "apps"})],
                monkeypatch) == 0


def test_cert_manager_tls_is_managed(monkeypatch):
    cert = _secret(
        name="example-com-tls",
        labels={"controller.cert-manager.io/fao": "true"},
        annotations={"cert-manager.io/certificate-name": "example-com"},
    )
    assert _run([cert], monkeypatch) == 0


def test_tailscale_device_state_is_managed(monkeypatch):
    assert _run([_secret(name="ts-x-0", ns="tailscale",
                         labels={"tailscale.com/managed": "true"})], monkeypatch) == 0


def test_an_allowlisted_secret_passes(monkeypatch):
    """The shipped ALLOWLIST is empty, so the entry is injected here."""
    monkeypatch.setitem(mod.ALLOWLIST, "external-secrets/op-credentials", "bootstrap")
    boot = _secret(name="op-credentials", ns="external-secrets")
    assert _run([boot], monkeypatch) == 0


def test_the_allowlist_is_exact_not_a_prefix(monkeypatch):
    """A near-miss name in the same namespace must still be flagged."""
    monkeypatch.setitem(mod.ALLOWLIST, "external-secrets/op-credentials", "bootstrap")
    impostor = _secret(name="op-credentials-old", ns="external-secrets")
    assert _run([impostor], monkeypatch) == 1


def test_the_shipped_allowlist_is_empty():
    """Site data: a generated cluster adds an entry only when it has to, and
    every entry is a claim that the value rotates somewhere outside the repo."""
    assert mod.ALLOWLIST == {}


def test_service_account_token_is_managed(monkeypatch):
    sa_token = _secret(name="sa-token", type="kubernetes.io/service-account-token")
    assert _run([sa_token], monkeypatch) == 0


def test_offender_message_lists_keys_not_values(monkeypatch, capsys):
    hand_applied = {
        "metadata": {"name": "mealie-secrets", "namespace": "recipes"},
        "data": {"openai-api-key": "c2VjcmV0", "smtp-password": "c2VjcmV0"},
    }
    assert _run([hand_applied], monkeypatch) == 1
    err = capsys.readouterr().err
    assert "openai-api-key" in err
    assert "c2VjcmV0" not in err
