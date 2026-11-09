"""authentik-auth-basic is authentik-auth plus the Authorization response header.

Traefik has no middleware inheritance, so the basic variant repeats the spec.
"""
from __future__ import annotations

import copy

import yaml
from conftest import REPO

MIDDLEWARES = REPO / "kubernetes" / "apps" / "authentik" / "middleware.yaml"


def test_basic_middleware_is_shared_plus_authorization():
    specs = {
        doc["metadata"]["name"]: doc["spec"]
        for doc in yaml.safe_load_all(MIDDLEWARES.read_text())
        if isinstance(doc, dict) and doc.get("kind") == "Middleware"
    }
    expected = copy.deepcopy(specs["authentik-auth"])
    headers = expected["forwardAuth"]["authResponseHeaders"]
    assert "Authorization" not in headers
    headers.append("Authorization")
    assert specs["authentik-auth-basic"] == expected
