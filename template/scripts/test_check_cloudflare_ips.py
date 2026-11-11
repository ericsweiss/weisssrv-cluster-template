"""check-cloudflare-ips.py: the Traefik trustedIPs list tracks Cloudflare's ranges."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-cloudflare-ips.py"
RELEASE = REPO / "kubernetes/infrastructure/controllers/traefik/release.yaml"


def _load():
    spec = importlib.util.spec_from_file_location("check_cloudflare_ips", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_release_pins_the_ranges_the_test_fixture_lists(tmp_path: Path) -> None:
    """The real release agrees with itself: pinned == a file of the same CIDRs."""
    mod = _load()
    pinned = mod.pinned_ranges(RELEASE)
    assert len(pinned) >= 20, "the websecure entrypoint must trust Cloudflare's v4 + v6 ranges"
    ranges = tmp_path / "ranges.txt"
    ranges.write_text("\n".join(pinned) + "\n")
    assert mod.main(["--ranges-file", str(ranges)]) == 0


def test_a_new_upstream_range_or_a_retired_one_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load()
    pinned = mod.pinned_ranges(RELEASE)
    ranges = tmp_path / "ranges.txt"
    ranges.write_text("\n".join(pinned[1:] + ["203.0.113.0/24"]) + "\n")
    assert mod.main(["--ranges-file", str(ranges)]) == 1
    out = capsys.readouterr().out
    assert "missing from the release: 203.0.113.0/24" in out
    assert f"no longer published by Cloudflare: {pinned[0]}" in out
    assert "            - 203.0.113.0/24" in out


def test_an_unreadable_source_is_exit_2(tmp_path: Path) -> None:
    mod = _load()
    assert mod.main(["--ranges-file", str(tmp_path / "absent.txt")]) == 2
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    assert mod.main(["--ranges-file", str(empty)]) == 2


def test_a_release_without_trusted_ips_is_exit_2(tmp_path: Path) -> None:
    mod = _load()
    release = tmp_path / "release.yaml"
    release.write_text("spec:\n  values:\n    ports:\n      websecure:\n        port: 443\n")
    ranges = tmp_path / "ranges.txt"
    ranges.write_text("198.41.128.0/17\n")
    assert mod.main(["--release", str(release), "--ranges-file", str(ranges)]) == 2
