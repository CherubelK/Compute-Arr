"""
Tests for the background job monitor: status/cost sync, provision-time
tracking, and failover for jobs that die shortly after launch.
Uses SQLite in-memory and stub adapters — no provider calls, no Postgres.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.cache import OfferCache, ProviderSnapshot
from app.config import settings
from app.models import FailoverEvent, Job
from app.monitor import sync_jobs_once
from app.providers.base import JobStatus, Offer, ProviderError
from app.stats import StatsTracker


@pytest.fixture(autouse=True)
def _monitor_env(db_session, monkeypatch):
    monkeypatch.setattr("app.monitor.SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "early_death_window_seconds", 300)
    monkeypatch.setattr(settings, "max_placement_attempts", 3)


@pytest.fixture()
def tracker(monkeypatch):
    t = StatsTracker()
    monkeypatch.setattr("app.monitor.stats_tracker", t)
    return t


def _seed_job(db_session, *, provider="runpod", status="pending", placed_seconds_ago=60.0, price=3.6) -> uuid.UUID:
    job = Job(
        id=uuid.uuid4(),
        gpu_class="H100",
        preference="cheapest",
        image="pytorch/pytorch:latest",
        baseline_provider="lambda",
        chosen_provider=provider,
        provider_job_id=f"{provider}-job-1",
        price_per_hr=price,
        placed_at=datetime.now(timezone.utc) - timedelta(seconds=placed_seconds_ago),
        status=status,
    )
    db_session.add(job)
    db_session.commit()
    return job.id


def _adapter(state: str, cost: float | None = None) -> MagicMock:
    adapter = MagicMock()
    adapter.status.side_effect = lambda pid: JobStatus(pid, state, cost_so_far=cost)
    return adapter


def _seed_offers(monkeypatch, prices: dict[str, float]) -> None:
    cache = OfferCache()
    for provider, price in prices.items():
        cache.update(ProviderSnapshot(
            provider_id=provider,
            healthy=True,
            offers=[Offer(provider=provider, gpu_class="H100", price_per_hr=price, available=True)],
            polled_at=datetime.now(timezone.utc),
        ))
    monkeypatch.setattr("app.router.offer_cache", cache)


# ---------------------------------------------------------------------------
# Status + cost sync
# ---------------------------------------------------------------------------

def test_sync_updates_status_and_cost(db_session, tracker):
    job_id = _seed_job(db_session, status="running")

    sync_jobs_once({"runpod": _adapter("succeeded", cost=1.25)})

    job = db_session.get(Job, job_id)
    assert job.status == "succeeded"
    assert float(job.actual_cost) == pytest.approx(1.25)


def test_sync_estimates_cost_when_provider_reports_none(db_session, tracker):
    # e.g. Lambda: $3.60/hr for 30 minutes
    job_id = _seed_job(db_session, provider="lambda", status="running", placed_seconds_ago=1800, price=3.6)

    sync_jobs_once({"lambda": _adapter("running", cost=None)})

    assert float(db_session.get(Job, job_id).actual_cost) == pytest.approx(1.8, rel=1e-2)


def test_sync_skips_finished_jobs(db_session, tracker):
    _seed_job(db_session, status="succeeded")
    adapter = _adapter("running")

    sync_jobs_once({"runpod": adapter})

    adapter.status.assert_not_called()


def test_sync_keeps_last_known_state_on_provider_error(db_session, tracker):
    job_id = _seed_job(db_session, status="running")
    adapter = MagicMock()
    adapter.status.side_effect = ProviderError("timeout")

    sync_jobs_once({"runpod": adapter})

    assert db_session.get(Job, job_id).status == "running"


def test_sync_records_provision_time_on_first_running(db_session, tracker):
    _seed_job(db_session, status="pending", placed_seconds_ago=45)
    adapter = _adapter("running")

    sync_jobs_once({"runpod": adapter})
    sync_jobs_once({"runpod": adapter})  # already running: must not be recorded again

    assert tracker.avg_provision_seconds("runpod", "H100") == pytest.approx(45, abs=5)
    assert len(tracker._data[("runpod", "H100")].provision_seconds) == 1


# ---------------------------------------------------------------------------
# Failover for jobs that die after launch
# ---------------------------------------------------------------------------

def test_early_death_fails_over_to_next_provider(db_session, tracker, monkeypatch):
    job_id = _seed_job(db_session, provider="runpod", placed_seconds_ago=60)
    _seed_offers(monkeypatch, {"runpod": 1.0, "vast": 2.0})
    runpod = _adapter("failed")
    vast = MagicMock()
    vast.submit.return_value = "vast-job-7"

    sync_jobs_once({"runpod": runpod, "vast": vast})

    job = db_session.get(Job, job_id)
    assert job.status == "pending"
    assert job.chosen_provider == "vast"
    assert job.provider_job_id == "vast-job-7"
    assert float(job.price_per_hr) == pytest.approx(2.0)

    event = db_session.query(FailoverEvent).one()
    assert (event.from_provider, event.to_provider) == ("runpod", "vast")
    assert "died" in event.reason

    runpod.submit.assert_not_called()  # never retried on the provider it died on
    runpod.cancel.assert_called_once_with("runpod-job-1")  # dead instance released


def test_late_failure_is_not_failed_over(db_session, tracker, monkeypatch):
    job_id = _seed_job(db_session, status="running", placed_seconds_ago=3600)
    _seed_offers(monkeypatch, {"runpod": 1.0, "vast": 2.0})
    vast = MagicMock()

    sync_jobs_once({"runpod": _adapter("failed"), "vast": vast})

    assert db_session.get(Job, job_id).status == "failed"
    vast.submit.assert_not_called()
    assert db_session.query(FailoverEvent).count() == 0


def test_early_death_failover_can_be_disabled(db_session, tracker, monkeypatch):
    monkeypatch.setattr(settings, "early_death_window_seconds", 0)
    job_id = _seed_job(db_session, placed_seconds_ago=5)
    _seed_offers(monkeypatch, {"runpod": 1.0, "vast": 2.0})
    vast = MagicMock()

    sync_jobs_once({"runpod": _adapter("failed"), "vast": vast})

    assert db_session.get(Job, job_id).status == "failed"
    vast.submit.assert_not_called()


def test_early_death_respects_attempt_cap(db_session, tracker, monkeypatch):
    # Two providers were already tried at placement; this death uses the last attempt.
    monkeypatch.setattr(settings, "max_placement_attempts", 3)
    job_id = _seed_job(db_session, provider="lambda", placed_seconds_ago=30)
    db_session.add_all([
        FailoverEvent(job_id=job_id, from_provider="runpod", to_provider="vast", reason="full"),
        FailoverEvent(job_id=job_id, from_provider="vast", to_provider="lambda", reason="full"),
    ])
    db_session.commit()
    _seed_offers(monkeypatch, {"runpod": 1.0, "vast": 2.0, "lambda": 3.0})
    runpod, vast = MagicMock(), MagicMock()

    sync_jobs_once({"runpod": runpod, "vast": vast, "lambda": _adapter("failed")})

    assert db_session.get(Job, job_id).status == "failed"
    runpod.submit.assert_not_called()
    vast.submit.assert_not_called()
    assert db_session.query(FailoverEvent).count() == 2


def test_early_death_with_no_alternative_marks_job_failed(db_session, tracker, monkeypatch):
    job_id = _seed_job(db_session, placed_seconds_ago=30)
    _seed_offers(monkeypatch, {"runpod": 1.0})
    runpod = _adapter("failed")

    sync_jobs_once({"runpod": runpod})

    assert db_session.get(Job, job_id).status == "failed"
    runpod.submit.assert_not_called()
    assert db_session.query(FailoverEvent).count() == 0


def test_cancelled_while_status_call_in_flight_is_left_alone(db_session, tracker, monkeypatch):
    # The provider reports "failed" for a job the user cancelled while the
    # status call was in flight. The cancel must win: no failover, no overwrite.
    job_id = _seed_job(db_session, placed_seconds_ago=30)
    _seed_offers(monkeypatch, {"runpod": 1.0, "vast": 2.0})
    vast = MagicMock()

    def status_then_cancel(pid):
        db_session.query(Job).filter(Job.id == job_id).update({"status": "cancelled"})
        return JobStatus(pid, "failed")

    runpod = MagicMock()
    runpod.status.side_effect = status_then_cancel

    sync_jobs_once({"runpod": runpod, "vast": vast})

    assert db_session.get(Job, job_id).status == "cancelled"
    vast.submit.assert_not_called()
