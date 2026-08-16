"""Unit tests for the cloudflare-ddns CronJob program.

The module lives beside its manifests
(kubernetes/infrastructure/configs/cloudflare-ddns/cloudflare-ddns.py) because
kustomize only accepts configMapGenerator sources inside the kustomization root,
so it is loaded by path here rather than imported.

What these cover is `update()`'s decision table, because every branch of it
writes — or refuses to write — a public DNS record, and the CronJob's only other
signal is its exit code. The Cloudflare calls are replaced with a fake `api`, so
nothing here touches the network.

The whole module is skipped when the cluster was generated with a different
`dns_backend`: there is then no DDNS module to test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = (
    REPO_ROOT / "kubernetes/infrastructure/configs/cloudflare-ddns/cloudflare-ddns.py"
)

pytestmark = pytest.mark.skipif(
    not MODULE_PATH.is_file(),
    reason="this cluster ships no cloudflare-ddns module (dns_backend)",
)


@pytest.fixture(scope="module")
def ddns():
    spec = importlib.util.spec_from_file_location("cloudflare_ddns", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_api(replies, seen):
    """Stand in for the module's `api`, recording (method, url, data)."""

    def api(url, method="GET", data=None):
        seen.append((method, url, data))
        reply = replies.pop(0)
        return reply(url, method, data) if callable(reply) else reply

    return api


def test_update_is_a_noop_when_the_address_is_unchanged(ddns, monkeypatch):
    """The common case: 5-minutely runs must not rewrite an unchanged record,
    which would burn API quota and churn the zone's change log."""
    seen = []
    replies = [{"success": True, "result": [{"id": "r1", "content": "198.51.100.7"}]}]
    monkeypatch.setattr(ddns, "api", _fake_api(replies, seen))
    assert ddns.update("z1", "vpn.zone.invalid", "198.51.100.7", False) is True
    assert [method for method, _, _ in seen] == ["GET"]


def test_update_puts_and_preserves_the_fields_terraform_owns(ddns, monkeypatch):
    """ttl and proxied seed CREATION only. Re-asserting this file's values on an
    existing record is what would make this job and Terraform flip-flop them on
    every cycle."""
    seen = []
    existing = {"id": "r1", "content": "198.51.100.1", "ttl": 60, "proxied": True,
                "comment": "Terraform-owned", "tags": ["managed"],
                "settings": {"ipv4_only": True}}
    replies = [{"success": True, "result": [existing]}, {"success": True}]
    monkeypatch.setattr(ddns, "api", _fake_api(replies, seen))
    assert ddns.update("z1", "git.zone.invalid", "198.51.100.7", False) is True
    method, url, data = seen[-1]
    assert method == "PUT"
    assert url.endswith("/dns_records/r1")
    assert data == {
        "type": "A",
        "name": "git.zone.invalid",
        "content": "198.51.100.7",
        "ttl": 60,
        "proxied": True,
        "comment": "Terraform-owned",
        "tags": ["managed"],
        "settings": {"ipv4_only": True},
    }


def test_update_posts_with_the_seed_values_when_the_record_is_absent(ddns, monkeypatch):
    seen = []
    replies = [{"success": True, "result": []}, {"success": True}]
    monkeypatch.setattr(ddns, "api", _fake_api(replies, seen))
    assert ddns.update("z1", "zone.invalid", "198.51.100.7", True) is True
    method, _, data = seen[-1]
    assert method == "POST"
    assert data["ttl"] == 1
    assert data["proxied"] is True


def test_update_refuses_to_write_when_the_query_failed(ddns, monkeypatch, capsys):
    """A failed query is indistinguishable from "no such record", and treating it
    as the latter would POST a duplicate. It must stop, and say so on stderr —
    stdout is the per-record report a successful run also writes."""
    seen = []
    monkeypatch.setattr(ddns, "api", _fake_api([None], seen))
    assert ddns.update("z1", "git.zone.invalid", "198.51.100.7", False) is False
    assert len(seen) == 1
    captured = capsys.readouterr()
    assert "cannot query" in captured.err
    assert "cannot query" not in captured.out


def test_update_reports_a_failed_write(ddns, monkeypatch, capsys):
    """A rejected PUT must not read as success: main()'s exit code is the only
    thing that turns a silently-stale record into a failed CronJob. The FAILED
    line goes to stderr, where the other failures are, so it is findable without
    parsing the per-record report."""
    seen = []
    replies = [
        {"success": True, "result": [{"id": "r1", "content": "198.51.100.1"}]},
        {"success": False},
    ]
    monkeypatch.setattr(ddns, "api", _fake_api(replies, seen))
    assert ddns.update("z1", "git.zone.invalid", "198.51.100.7", False) is False
    captured = capsys.readouterr()
    assert "FAILED" in captured.err
    assert "FAILED" not in captured.out


def test_records_are_parsed_as_name_colon_proxied(ddns, monkeypatch):
    """DDNS_RECORDS is `name[:proxied]`, and only an explicit `:false` turns the
    proxy off — a mis-parse silently publishes an origin address."""
    calls = []
    monkeypatch.setattr(ddns, "TOKEN", "tok")
    monkeypatch.setattr(ddns, "ZONE", "zone.invalid")
    monkeypatch.setattr(ddns, "RECORDS", "zone.invalid, direct.zone.invalid:false")
    monkeypatch.setattr(ddns, "public_ip", lambda: "198.51.100.7")
    monkeypatch.setattr(
        ddns, "api", lambda *a, **k: {"success": True, "result": [{"id": "z1"}]}
    )
    monkeypatch.setattr(
        ddns,
        "update",
        lambda zone_id, name, current, proxied: calls.append((name, proxied)) or True,
    )
    assert ddns.main() == 0
    assert calls == [("zone.invalid", True), ("direct.zone.invalid", False)]

def test_multiple_records_refuse_partial_update(ddns, monkeypatch, capsys):
    """Two A records for one name = ambiguous ownership; a partial update
    leaves the sibling answering stale intermittently."""
    seen = []
    replies = [{"success": True, "result": [
        {"id": "r1", "content": "198.51.100.1"},
        {"id": "r2", "content": "198.51.100.2"},
    ]}]
    monkeypatch.setattr(ddns, "api", _fake_api(replies, seen))
    assert ddns.update("z1", "vpn.zone.invalid", "203.0.113.9", False) is False
    assert "multiple A records" in capsys.readouterr().err
    assert [method for method, _, _ in seen] == ["GET"]

def test_empty_or_blank_records_fail_closed(ddns, monkeypatch):
    """An empty list exits 0 managing nothing; a nameless entry queries wide."""
    monkeypatch.setattr(ddns, "TOKEN", "t")
    monkeypatch.setattr(ddns, "ZONE", "zone.invalid")
    for bad in ("", "  ", ":false", "a.zone.invalid,:true"):
        monkeypatch.setattr(ddns, "RECORDS", bad)
        with pytest.raises(SystemExit) as e:
            ddns.main()
        assert "DDNS_RECORDS" in str(e.value)

