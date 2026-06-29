"""Integration tests for POST /jobs, GET /jobs/{id}, GET /jobs/{id}/logs."""
import uuid
from unittest.mock import MagicMock

import pytest

from app.cache import OfferCache, ProviderSnapshot
from app.dependencies import get_adapters
from app.main import app
from app.providers.base import JobStatus, Offer, ProviderError

from datetime import datetime, timezone


def _offer(provider: str, price: float = 2.0) -> Offer:
    return Offer(provider=provider, gpu_class="H100", price_per_hr=price, available=True)


def _snap(provider: str, offers: list) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider_id=provider,
        healthy=True,
        offers=offers,
        polled_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# POST /jobs
# ---------------------------------------------------------------------------

def test_submit_job_success(client, monkeypatch):
    # Seed cache so the router has something to pick
    cache = OfferCache()
    cache.update(_snap("runpod", [_offer("runpod", 2.49)]))
    monkeypatch.setattr("app.router.offer_cache", cache)

    # Mock adapter that succeeds
    mock_adapter = MagicMock()
    mock_adapter.submit.return_value = "pod-abc123"
    app.dependency_overrides[get_adapters] = lambda: {"runpod": mock_adapter}

    resp = client.post("/jobs", json={
        "gpu_class": "H100",
        "preference": "cheapest",
        "image": "pytorch/pytorch:latest",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["chosen_provider"] == "runpod"
    assert body["status"] == "running"
    assert "job_id" in body
    assert body["price_per_hr"] == 2.49

    app.dependency_overrides.pop(get_adapters, None)


def test_submit_job_no_cache_returns_503(client, monkeypatch):
    cache = OfferCache()  # empty
    monkeypatch.setattr("app.router.offer_cache", cache)

    resp = client.post("/jobs", json={
        "gpu_class": "H100",
        "preference": "cheapest",
        "image": "pytorch/pytorch:latest",
    })
    assert resp.status_code == 503


def test_submit_job_failover_to_second_provider(client, monkeypatch):
    cache = OfferCache()
    cache.update(_snap("runpod", [_offer("runpod", 1.0)]))
    cache.update(_snap("vast", [_offer("vast", 2.0)]))
    monkeypatch.setattr("app.router.offer_cache", cache)

    failing = MagicMock()
    failing.submit.side_effect = ProviderError("placement failed")
    succeeding = MagicMock()
    succeeding.submit.return_value = "vast-job-99"

    app.dependency_overrides[get_adapters] = lambda: {"runpod": failing, "vast": succeeding}

    resp = client.post("/jobs", json={
        "gpu_class": "H100",
        "preference": "cheapest",
        "image": "nginx:latest",
    })
    assert resp.status_code == 201
    assert resp.json()["chosen_provider"] == "vast"

    app.dependency_overrides.pop(get_adapters, None)


def test_submit_job_all_providers_fail_returns_503(client, monkeypatch):
    cache = OfferCache()
    cache.update(_snap("runpod", [_offer("runpod", 1.0)]))
    monkeypatch.setattr("app.router.offer_cache", cache)

    failing = MagicMock()
    failing.submit.side_effect = ProviderError("always fails")
    app.dependency_overrides[get_adapters] = lambda: {"runpod": failing}

    resp = client.post("/jobs", json={
        "gpu_class": "H100",
        "preference": "cheapest",
        "image": "nginx:latest",
    })
    assert resp.status_code == 503

    app.dependency_overrides.pop(get_adapters, None)


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------

def test_get_job_not_found(client):
    resp = client.get(f"/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_job_returns_current_status(client, monkeypatch):
    # First, create a job via POST
    cache = OfferCache()
    cache.update(_snap("runpod", [_offer("runpod", 2.49)]))
    monkeypatch.setattr("app.router.offer_cache", cache)

    mock_adapter = MagicMock()
    mock_adapter.submit.return_value = "pod-xyz"
    mock_adapter.status.return_value = JobStatus(
        provider_job_id="pod-xyz", state="running", cost_so_far=0.42
    )
    app.dependency_overrides[get_adapters] = lambda: {"runpod": mock_adapter}

    post_resp = client.post("/jobs", json={
        "gpu_class": "H100",
        "preference": "cheapest",
        "image": "pytorch/pytorch:latest",
    })
    job_id = post_resp.json()["job_id"]

    get_resp = client.get(f"/jobs/{job_id}")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["job_id"] == job_id
    assert data["status"] == "running"
    assert data["actual_cost"] == pytest.approx(0.42)

    app.dependency_overrides.pop(get_adapters, None)


# ---------------------------------------------------------------------------
# GET /jobs/{id}/logs
# ---------------------------------------------------------------------------

def test_get_job_logs(client, monkeypatch):
    cache = OfferCache()
    cache.update(_snap("runpod", [_offer("runpod", 2.49)]))
    monkeypatch.setattr("app.router.offer_cache", cache)

    mock_adapter = MagicMock()
    mock_adapter.submit.return_value = "pod-log-test"
    mock_adapter.status.return_value = JobStatus("pod-log-test", "running")
    mock_adapter.logs.return_value = "Epoch 1/10 loss=0.8\n"
    app.dependency_overrides[get_adapters] = lambda: {"runpod": mock_adapter}

    post_resp = client.post("/jobs", json={
        "gpu_class": "H100",
        "preference": "cheapest",
        "image": "pytorch/pytorch:latest",
    })
    job_id = post_resp.json()["job_id"]

    log_resp = client.get(f"/jobs/{job_id}/logs")
    assert log_resp.status_code == 200
    assert "Epoch 1/10" in log_resp.json()["logs"]

    app.dependency_overrides.pop(get_adapters, None)
