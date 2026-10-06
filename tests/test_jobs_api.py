"""Integration tests for POST /jobs, GET /jobs/{id}, cancel, and logs."""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.cache import OfferCache, ProviderSnapshot
from app.dependencies import get_adapters
from app.main import app
from app.models import FailoverEvent, Job, RoutingDecision
from app.providers.base import JobStatus, Offer, ProviderError

_JOB = {"gpu_class": "H100", "preference": "cheapest", "image": "pytorch/pytorch:latest"}


def _offer(provider: str, price: float = 2.0) -> Offer:
    return Offer(provider=provider, gpu_class="H100", price_per_hr=price, available=True)


def _snap(provider: str, offers: list) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider_id=provider,
        healthy=True,
        offers=offers,
        polled_at=datetime.now(timezone.utc),
    )


def _seed_cache(monkeypatch, offers_by_provider: dict[str, list[Offer]]) -> None:
    cache = OfferCache()
    for provider, offers in offers_by_provider.items():
        cache.update(_snap(provider, offers))
    monkeypatch.setattr("app.router.offer_cache", cache)


def _use_adapters(adapters: dict) -> None:
    # The client fixture clears dependency overrides on teardown.
    app.dependency_overrides[get_adapters] = lambda: adapters


def _adapter(provider_job_id: str = "pod-1") -> MagicMock:
    adapter = MagicMock()
    adapter.submit.return_value = provider_job_id
    return adapter


def _failing_adapter(error: str = "placement failed") -> MagicMock:
    adapter = MagicMock()
    adapter.submit.side_effect = ProviderError(error)
    return adapter


# ---------------------------------------------------------------------------
# POST /jobs
# ---------------------------------------------------------------------------

def test_submit_job_success(client, db_session, monkeypatch):
    _seed_cache(monkeypatch, {"runpod": [_offer("runpod", 2.49)]})
    _use_adapters({"runpod": _adapter("pod-abc123")})

    resp = client.post("/jobs", json=_JOB)
    assert resp.status_code == 201
    body = resp.json()
    assert body["chosen_provider"] == "runpod"
    assert body["status"] == "pending"  # placed; the monitor flips it to running
    assert body["price_per_hr"] == 2.49

    job = db_session.get(Job, uuid.UUID(body["job_id"]))
    assert job.provider_job_id == "pod-abc123"
    assert float(job.price_per_hr) == pytest.approx(2.49)
    assert job.placed_at is not None

    decision = db_session.query(RoutingDecision).one()
    assert decision.chosen_provider == "runpod"
    assert len(decision.ranked_offers) == 1


def test_submit_job_no_cache_returns_503(client, monkeypatch):
    _seed_cache(monkeypatch, {})

    resp = client.post("/jobs", json=_JOB)
    assert resp.status_code == 503


def test_submit_job_failover_to_second_provider(client, db_session, monkeypatch):
    _seed_cache(monkeypatch, {"runpod": [_offer("runpod", 1.0)], "vast": [_offer("vast", 2.0)]})
    _use_adapters({"runpod": _failing_adapter("no capacity"), "vast": _adapter("vast-job-99")})

    resp = client.post("/jobs", json=_JOB)
    assert resp.status_code == 201
    body = resp.json()
    assert body["chosen_provider"] == "vast"
    assert body["price_per_hr"] == 2.0
    assert "failed over to vast" in body["reasoning"]

    event = db_session.query(FailoverEvent).one()
    assert (event.from_provider, event.to_provider) == ("runpod", "vast")
    assert event.reason == "no capacity"

    # The decision keeps the router's pick; the job records where it actually ran.
    assert db_session.query(RoutingDecision).one().chosen_provider == "runpod"
    assert db_session.query(Job).one().chosen_provider == "vast"


def test_submit_job_fails_over_past_a_provider_holding_the_top_offers(client, db_session, monkeypatch):
    # Vast holds the three cheapest offers. Failover must still reach RunPod
    # rather than spending every attempt on Vast.
    _seed_cache(monkeypatch, {
        "vast": [_offer("vast", 1.0), _offer("vast", 1.1), _offer("vast", 1.2)],
        "runpod": [_offer("runpod", 2.5)],
    })
    vast = _failing_adapter()
    _use_adapters({"vast": vast, "runpod": _adapter("pod-1")})

    resp = client.post("/jobs", json=_JOB)
    assert resp.status_code == 201
    assert resp.json()["chosen_provider"] == "runpod"
    assert vast.submit.call_count == 1

    event = db_session.query(FailoverEvent).one()
    assert (event.from_provider, event.to_provider) == ("vast", "runpod")


def test_submit_job_all_providers_fail_returns_503(client, db_session, monkeypatch):
    _seed_cache(monkeypatch, {"runpod": [_offer("runpod", 1.0)]})
    _use_adapters({"runpod": _failing_adapter("always fails")})

    resp = client.post("/jobs", json=_JOB)
    assert resp.status_code == 503

    # The attempt is still on record: a failed job and the decision behind it.
    assert db_session.query(Job).one().status == "failed"
    assert db_session.query(RoutingDecision).count() == 1


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------

def test_get_job_not_found(client):
    resp = client.get(f"/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_job_malformed_id_returns_422(client):
    resp = client.get("/jobs/not-a-uuid")
    assert resp.status_code == 422


def test_get_job_returns_stored_state(client, monkeypatch):
    _seed_cache(monkeypatch, {"runpod": [_offer("runpod", 2.49)]})
    adapter = _adapter("pod-xyz")
    _use_adapters({"runpod": adapter})

    job_id = client.post("/jobs", json=_JOB).json()["job_id"]

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["status"] == "pending"
    assert data["chosen_provider"] == "runpod"
    assert data["price_per_hr"] == pytest.approx(2.49)
    adapter.status.assert_not_called()  # reads come from the DB, not the provider


# ---------------------------------------------------------------------------
# POST /jobs/{id}/cancel
# ---------------------------------------------------------------------------

def test_cancel_job(client, monkeypatch):
    _seed_cache(monkeypatch, {"runpod": [_offer("runpod", 2.49)]})
    adapter = _adapter("pod-xyz")
    adapter.status.return_value = JobStatus("pod-xyz", "running", cost_so_far=0.42)
    _use_adapters({"runpod": adapter})

    job_id = client.post("/jobs", json=_JOB).json()["job_id"]

    resp = client.post(f"/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"
    assert data["actual_cost"] == pytest.approx(0.42)  # final cost captured before cancelling
    adapter.cancel.assert_called_once_with("pod-xyz")


def test_cancel_job_not_found(client):
    resp = client.post(f"/jobs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


def test_cancel_finished_job_returns_409(client, monkeypatch):
    _seed_cache(monkeypatch, {"runpod": [_offer("runpod", 2.49)]})
    adapter = _adapter("pod-xyz")
    adapter.status.return_value = JobStatus("pod-xyz", "running")
    _use_adapters({"runpod": adapter})

    job_id = client.post("/jobs", json=_JOB).json()["job_id"]
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 200

    resp = client.post(f"/jobs/{job_id}/cancel")
    assert resp.status_code == 409
    assert adapter.cancel.call_count == 1


def test_cancel_job_provider_error_returns_502_and_keeps_job_active(client, monkeypatch):
    _seed_cache(monkeypatch, {"runpod": [_offer("runpod", 2.49)]})
    adapter = _adapter("pod-xyz")
    adapter.status.return_value = JobStatus("pod-xyz", "running")
    adapter.cancel.side_effect = ProviderError("provider unreachable")
    _use_adapters({"runpod": adapter})

    job_id = client.post("/jobs", json=_JOB).json()["job_id"]

    assert client.post(f"/jobs/{job_id}/cancel").status_code == 502
    assert client.get(f"/jobs/{job_id}").json()["status"] == "pending"


# ---------------------------------------------------------------------------
# GET /jobs/{id}/logs
# ---------------------------------------------------------------------------

def test_get_job_logs(client, monkeypatch):
    _seed_cache(monkeypatch, {"runpod": [_offer("runpod", 2.49)]})
    adapter = _adapter("pod-log-test")
    adapter.logs.return_value = "Epoch 1/10 loss=0.8\n"
    _use_adapters({"runpod": adapter})

    job_id = client.post("/jobs", json=_JOB).json()["job_id"]

    resp = client.get(f"/jobs/{job_id}/logs")
    assert resp.status_code == 200
    assert "Epoch 1/10" in resp.json()["logs"]
