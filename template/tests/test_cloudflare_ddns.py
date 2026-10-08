"""`update()`'s decision table in the cloudflare-ddns CronJob program: which
branches write a public DNS record and which refuse. The program is loaded by
path from kubernetes/infrastructure/configs/cloudflare-ddns/, with a fake api."""

from __future__ import annotations

import pytest
from conftest import REPO as REPO_ROOT
from conftest import load_script

MODULE_PATH = (
    REPO_ROOT / "kubernetes/infrastructure/configs/cloudflare-ddns/cloudflare-ddns.py"
)

pytestmark = pytest.mark.skipif(
    not MODULE_PATH.is_file(),
    reason="this cluster ships no cloudflare-ddns module (dns_backend)",
)


@pytest.fixture(scope="module")
def ddns():
    return load_script(MODULE_PATH)


def _fake_api(replies, seen):
    """Stand in for the module's `api`, recording (method, url, data)."""

    def api(url, method="GET", data=None):
        seen.append((method, url, data))
        if not replies:
            raise AssertionError("unexpected extra API call: %s %s" % (method, url))
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
    """A rejected PUT exits non-zero and reports FAILED on stderr."""
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


def test_update_percent_encodes_the_record_name_in_the_query(ddns, monkeypatch):
    """A raw `&` or `#` in the name would truncate `?type=A&name=`, so the GET
    matches a different record set and the write lands on that record."""
    seen = []
    replies = [{"success": True, "result": [{"id": "r1", "content": "198.51.100.7"}]}]
    monkeypatch.setattr(ddns, "api", _fake_api(replies, seen))
    assert ddns.update("z1", "a&b.zone.invalid", "198.51.100.7", False) is True
    url = seen[0][1]
    assert "name=a%26b.zone.invalid" in url
    assert "&" not in url.split("name=", 1)[1]


def test_main_percent_encodes_the_zone_in_the_lookup(ddns, monkeypatch):
    """Same truncation on the zone lookup: a raw `&` ends the `?name=` value and
    whatever zone comes back is adopted as this zone's id."""
    seen = []
    monkeypatch.setattr(ddns, "TOKEN", "tok")
    monkeypatch.setattr(ddns, "ZONE", "a&b.invalid")
    monkeypatch.setattr(ddns, "RECORDS", "host.a&b.invalid")
    monkeypatch.setattr(ddns, "public_ip", lambda: "198.51.100.7")
    monkeypatch.setattr(
        ddns, "api", _fake_api([{"success": True, "result": [{"id": "z1"}]}], seen)
    )
    monkeypatch.setattr(ddns, "update", lambda *a, **k: True)
    assert ddns.main() == 0
    url = seen[0][1]
    assert "/zones?name=a%26b.invalid" in url
    assert "&" not in url.split("name=", 1)[1]


def test_empty_or_blank_records_fail_closed(ddns, monkeypatch):
    """An empty list exits 0 managing nothing; a nameless entry queries wide."""
    monkeypatch.setattr(ddns, "TOKEN", "t")
    monkeypatch.setattr(ddns, "ZONE", "zone.invalid")
    for bad in ("", "  ", ":false", "a.zone.invalid,:true"):
        monkeypatch.setattr(ddns, "RECORDS", bad)
        with pytest.raises(SystemExit) as e:
            ddns.main()
        assert "DDNS_RECORDS" in str(e.value)


def _records_exit(ddns, monkeypatch, records):
    """main()'s message for a DDNS_RECORDS value, asserted to fail before the
    first API call: the guards run ahead of IP detection and the zone lookup."""
    monkeypatch.setattr(ddns, "TOKEN", "t")
    monkeypatch.setattr(ddns, "ZONE", "zone.invalid")
    monkeypatch.setattr(ddns, "RECORDS", records)
    monkeypatch.setattr(
        ddns, "public_ip", lambda: pytest.fail("reached public_ip() past the guard")
    )
    monkeypatch.setattr(
        ddns, "api", lambda *a, **k: pytest.fail("reached the API past the guard")
    )
    with pytest.raises(SystemExit) as e:
        ddns.main()
    return str(e.value)


def test_records_reject_malformed_entry_shapes(ddns, monkeypatch):
    """Record entries are shape-validated before content, so `a:false:oops` is
    rejected rather than read as a proxied record."""
    for bad in ("a.zone.invalid:false:oops", "a.zone.invalid:true:", "::"):
        assert "not a name[:proxied] pair" in _records_exit(ddns, monkeypatch, bad)
    # A well-formed sibling does not excuse a malformed one.
    assert "not a name[:proxied] pair" in _records_exit(
        ddns, monkeypatch, "ok.zone.invalid:true, bad.zone.invalid:true:oops"
    )


def test_records_reject_a_non_boolean_proxied_flag(ddns, monkeypatch):
    """Only `false` ever turned the proxy off, so `:no`, `:0` and a bare trailing
    colon all silently left the record proxied through Cloudflare."""
    for bad in ("a.zone.invalid:no", "a.zone.invalid:0", "a.zone.invalid:yes",
                "a.zone.invalid:"):
        assert "non-boolean proxied flag" in _records_exit(ddns, monkeypatch, bad)


def test_records_accept_the_documented_spellings(ddns, monkeypatch):
    """The shape checks must not tighten the contract the cluster-config default
    (`<external_domain>:true`) and the omitted-flag form already rely on."""
    calls = []
    monkeypatch.setattr(ddns, "TOKEN", "tok")
    monkeypatch.setattr(ddns, "ZONE", "zone.invalid")
    monkeypatch.setattr(
        ddns, "RECORDS", "zone.invalid:true, bare.zone.invalid, up.zone.invalid:FALSE"
    )
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
    assert calls == [
        ("zone.invalid", True),
        ("bare.zone.invalid", True),
        ("up.zone.invalid", False),
    ]



# --------------------------------------------------------------------------
# public_ip(): the guard that keeps an unreachable A record off the zone
# --------------------------------------------------------------------------


# A genuinely GLOBAL address: Python's is_global is false for every
# documentation range, so 198.51.100.x would be rejected by the guard itself.
GLOBAL_IP = "93.184.216.34"


class _Resp:
    def __init__(self, body):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(answers, seen):
    """Stand in for urllib.request.urlopen, one answer per provider URL."""

    def urlopen(url, timeout=None):
        seen.append(url)
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        return _Resp(answer)

    return urlopen


def test_a_private_or_ipv6_answer_is_skipped(ddns, monkeypatch):
    """A captive portal answers with RFC1918 and a dual-stack provider with
    AAAA; publishing either as an A record points the name at nothing."""
    seen = []
    providers = ("https://one.invalid", "https://two.invalid", "https://three.invalid")
    answers = {
        providers[0]: "10.0.0.1",
        providers[1]: "2001:db8::1",
        providers[2]: GLOBAL_IP,
    }
    monkeypatch.setattr(
        ddns.urllib.request, "urlopen", _urlopen_returning(answers, seen)
    )
    assert ddns.public_ip(providers=providers, sleep=lambda _s: None) == GLOBAL_IP
    assert seen == list(providers)


def test_the_first_global_answer_wins(ddns, monkeypatch):
    """Each extra provider is a network round-trip on a 5-minutely job."""
    seen = []
    providers = ("https://one.invalid", "https://two.invalid")
    monkeypatch.setattr(
        ddns.urllib.request,
        "urlopen",
        _urlopen_returning({providers[0]: GLOBAL_IP}, seen),
    )
    assert ddns.public_ip(providers=providers, sleep=lambda _s: None) == GLOBAL_IP
    assert seen == [providers[0]]


def test_every_provider_failing_returns_none_after_the_retries(ddns, monkeypatch):
    seen = []
    slept = []
    providers = ("https://one.invalid",)
    monkeypatch.setattr(
        ddns.urllib.request,
        "urlopen",
        _urlopen_returning({providers[0]: OSError("unreachable")}, seen),
    )
    assert ddns.public_ip(providers=providers, attempts=3, sleep=slept.append) is None
    assert len(seen) == 3
    assert slept == [5, 5], "a failed round must back off, but not after the last one"


def test_main_reports_failure_when_one_record_fails(ddns, monkeypatch):
    """The CronJob's only signal is its exit code, so one failed record must
    not be reported as a clean run."""
    monkeypatch.setattr(ddns, "TOKEN", "t")
    monkeypatch.setattr(ddns, "ZONE", "zone.invalid")
    monkeypatch.setattr(ddns, "RECORDS", "a.zone.invalid,b.zone.invalid")
    monkeypatch.setattr(ddns, "public_ip", lambda *a, **k: GLOBAL_IP)
    monkeypatch.setattr(
        ddns, "api", lambda *a, **k: {"success": True, "result": [{"id": "z1"}]}
    )
    monkeypatch.setattr(
        ddns, "update", lambda zone_id, name, current, proxied: name == "a.zone.invalid"
    )
    assert ddns.main() == 1
