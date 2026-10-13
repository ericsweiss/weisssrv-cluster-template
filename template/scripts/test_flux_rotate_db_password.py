"""Tests for scripts/flux-rotate-db-password.sh: the 1Password item first, then
the role, then the refresh, with every new password kept off every argv."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "flux-rotate-db-password.sh"
VALUE_RE = re.compile(r"^[A-Za-z0-9]{40}$")

# Every fake appends its basename to `order` and its arguments to `<name>.argv`;
# the two that are fed on stdin keep it in `<name>.stdin`.
FAKES = {
    "op": """#!/usr/bin/env bash
echo op >> "$FAKE_LOG/order"
printf '%s\\n' "$*" >> "$FAKE_LOG/op.argv"
case "$1 $2" in
    "item get") printf '%s' "$FAKE_ITEM" ;;
    "item edit") cat > "$FAKE_LOG/op-edit.stdin" ;;
esac
""",
    "kubectl": """#!/usr/bin/env bash
echo kubectl >> "$FAKE_LOG/order"
printf '%s\\n' "$*" >> "$FAKE_LOG/kubectl.argv"
cat > "$FAKE_LOG/kubectl.stdin"
exit "${FAKE_KUBECTL_RC:-0}"
""",
    "task": """#!/usr/bin/env bash
echo task >> "$FAKE_LOG/order"
printf '%s\\n' "$*" >> "$FAKE_LOG/task.argv"
""",
}

CASES = {
    "authentik": {
        "item": "Authentik Secrets",
        "fields": ["secret-key", "postgresql-password", "postgresql-admin-password", "admin-password"],
        "roles": {"authentik": "postgresql-password", "postgres": "postgresql-admin-password"},
        "exec_prefix": "exec -i -n authentik authentik-postgresql-0 -c postgresql -- sh -c",
        "rotate": "flux:rotate-secret -- authentik/authentik-secrets",
    },
}


def _item(title: str, fields: list[str]) -> str:
    built = [
        {"id": "password", "label": "password", "type": "CONCEALED", "purpose": "PASSWORD", "value": "old-primary"},
        {"id": "notesPlain", "label": "notesPlain", "type": "STRING", "purpose": "NOTES", "value": "keep me"},
    ]
    built += [{"id": f"id-{f}", "label": f, "type": "CONCEALED", "value": f"old-{f}"} for f in fields]
    return json.dumps({"id": "item-id", "title": title, "category": "PASSWORD", "fields": built})


def _run(tmp_path: Path, *args: str, kubectl_rc: int = 0, case: dict | None = None) -> tuple[subprocess.CompletedProcess, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    for name, body in FAKES.items():
        path = fake_bin / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "log"
    log.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_LOG"] = str(log)
    env["FAKE_KUBECTL_RC"] = str(kubectl_rc)
    env["FAKE_ITEM"] = _item(case["item"], case["fields"]) if case else "{}"
    res = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)
    return res, log


def _edited_values(log: Path) -> dict[str, str]:
    item = json.loads((log / "op-edit.stdin").read_text())
    return {f["label"]: f.get("value") for f in item["fields"]}


@pytest.mark.parametrize("app", sorted(CASES))
def test_a_rotation_runs_item_then_role_then_refresh(tmp_path, app):
    case = CASES[app]
    res, log = _run(tmp_path, app, case=case)
    assert res.returncode == 0, res.stderr
    assert (log / "order").read_text().split() == ["op", "op", "kubectl", "task"]
    assert (log / "task.argv").read_text().strip() == case["rotate"]


@pytest.mark.parametrize("app", sorted(CASES))
def test_the_item_edit_rides_the_template_and_mirrors_the_primary(tmp_path, app):
    case = CASES[app]
    _, log = _run(tmp_path, app, case=case)
    # Both op calls sit in one pipeline, so their start order is not fixed.
    assert set((log / "op.argv").read_text().splitlines()) == {
        f'item get {case["item"]} --vault Homelab --format json',
        f'item edit {case["item"]} --vault Homelab',
    }
    values = _edited_values(log)
    for field in case["roles"].values():
        assert VALUE_RE.match(values[field]), field
    first_field = next(iter(case["roles"].values()))
    assert values["password"] == values[first_field]
    assert values["notesPlain"] == "keep me"
    for field in case["fields"]:
        if field not in case["roles"].values():
            assert values[field] == f"old-{field}", field


@pytest.mark.parametrize("app", sorted(CASES))
def test_the_sql_carries_a_verifier_for_every_role_on_stdin(tmp_path, app):
    case = CASES[app]
    _, log = _run(tmp_path, app, case=case)
    values = _edited_values(log)
    (argv,) = (log / "kubectl.argv").read_text().splitlines()
    assert argv.startswith(case["exec_prefix"]), argv
    assert "-v ON_ERROR_STOP=1 --single-transaction -f -" in argv
    sql = (log / "kubectl.stdin").read_text()
    for role, field in case["roles"].items():
        verifier = re.search(rf"ALTER USER {role} PASSWORD '([^']+)';", sql).group(1)
        assert _scram_matches(values[field], verifier), role
        assert values[field] not in sql
        assert values[field] not in argv


def _scram_matches(password: str, verifier: str) -> bool:
    """The verifier's stored key, recomputed from the cleartext and its salt."""
    head, keys = verifier.split("$")[1:]
    iterations, salt = head.split(":")
    stored_key = keys.split(":")[0]
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    return base64.b64encode(hashlib.sha256(client_key).digest()).decode() == stored_key


@pytest.mark.parametrize("app", sorted(CASES))
def test_a_refused_password_stops_before_the_refresh(tmp_path, app):
    case = CASES[app]
    res, log = _run(tmp_path, app, kubectl_rc=1, case=case)
    assert res.returncode == 1
    assert "commit state" in res.stderr and "item history" in res.stderr
    assert (log / "op-edit.stdin").exists()
    assert not (log / "task.argv").exists()


@pytest.mark.parametrize("app", sorted(CASES))
def test_a_field_missing_from_the_item_stops_before_any_change(tmp_path, app):
    case = dict(CASES[app])
    case["fields"] = [f for f in case["fields"] if f != next(iter(case["roles"].values()))]
    res, log = _run(tmp_path, app, case=case)
    assert res.returncode != 0
    assert "missing or ambiguous 1Password fields" in res.stderr
    assert not (log / "kubectl.argv").exists()
    assert not (log / "task.argv").exists()


@pytest.mark.parametrize("args", [(), ("bogus",)])
def test_an_unknown_app_is_a_usage_error(tmp_path, args):
    res, log = _run(tmp_path, *args)
    assert res.returncode == 2
    assert "usage:" in res.stderr
    assert not (log / "order").exists()
