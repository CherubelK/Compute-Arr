"""API key enforcement. Other test modules run with auth overridden; these don't."""
import uuid

import pytest

from app.auth import require_api_key
from app.config import settings
from app.main import app

_PROTECTED = [
    ("GET", "/providers"),
    ("GET", "/usage"),
    ("POST", "/jobs"),
    ("GET", f"/jobs/{uuid.uuid4()}"),
    ("GET", f"/jobs/{uuid.uuid4()}/logs"),
    ("POST", f"/jobs/{uuid.uuid4()}/cancel"),
]


@pytest.fixture()
def secured_client(client, monkeypatch):
    app.dependency_overrides.pop(require_api_key)
    monkeypatch.setattr(settings, "api_key", "test-key")
    return client


@pytest.mark.parametrize(("method", "path"), _PROTECTED)
def test_missing_key_is_rejected(secured_client, method, path):
    assert secured_client.request(method, path).status_code == 401


@pytest.mark.parametrize(("method", "path"), _PROTECTED)
def test_wrong_key_is_rejected(secured_client, method, path):
    resp = secured_client.request(method, path, headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_valid_key_is_accepted(secured_client):
    resp = secured_client.get("/usage", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200


def test_health_needs_no_key(secured_client):
    assert secured_client.get("/health").status_code == 200
