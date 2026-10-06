"""Unit tests for placement failover (stub adapters, SQLite in-memory)."""
import uuid
from unittest.mock import MagicMock

import pytest

from app.models import FailoverEvent, Job
from app.placement import best_offer_per_provider, place_with_failover
from app.providers.base import JobSpec, Offer, ProviderError

_SPEC = JobSpec(gpu_class="H100", preference="cheapest", image="pytorch/pytorch:latest")


def _offer(provider: str, price: float) -> Offer:
    return Offer(provider=provider, gpu_class="H100", price_per_hr=price, available=True)


def _ok(provider_job_id: str) -> MagicMock:
    adapter = MagicMock()
    adapter.submit.return_value = provider_job_id
    return adapter


def _failing(error: str = "no capacity") -> MagicMock:
    adapter = MagicMock()
    adapter.submit.side_effect = ProviderError(error)
    return adapter


@pytest.fixture()
def job_id(db_session):
    job = Job(
        id=uuid.uuid4(),
        gpu_class="H100",
        preference="cheapest",
        image="pytorch/pytorch:latest",
        baseline_provider="lambda",
    )
    db_session.add(job)
    db_session.flush()
    return job.id


def _events(db_session) -> list[tuple[str, str]]:
    rows = db_session.query(FailoverEvent).order_by(FailoverEvent.id).all()
    return [(e.from_provider, e.to_provider) for e in rows]


def test_best_offer_per_provider_keeps_rank_order():
    ranked = [_offer("vast", 1.0), _offer("vast", 1.1), _offer("runpod", 2.0), _offer("vast", 2.5), _offer("lambda", 3.0)]
    candidates = best_offer_per_provider(ranked)
    assert [(o.provider, o.price_per_hr) for o in candidates] == [("vast", 1.0), ("runpod", 2.0), ("lambda", 3.0)]


def test_best_offer_per_provider_honours_exclude():
    ranked = [_offer("vast", 1.0), _offer("runpod", 2.0)]
    assert [o.provider for o in best_offer_per_provider(ranked, exclude={"vast"})] == ["runpod"]


def test_first_provider_success_logs_no_failover(db_session, job_id):
    adapters = {"vast": _ok("v-1"), "runpod": _ok("r-1")}
    placement = place_with_failover(
        [_offer("vast", 1.0), _offer("runpod", 2.0)], _SPEC, adapters, db_session, job_id, max_attempts=3
    )
    assert placement.provider_job_id == "v-1"
    assert placement.offer.provider == "vast"
    adapters["runpod"].submit.assert_not_called()
    assert _events(db_session) == []


def test_walks_providers_in_rank_order_and_logs_each_hop(db_session, job_id):
    adapters = {"vast": _failing("vast down"), "runpod": _failing("runpod full"), "lambda": _ok("l-1")}
    ranked = [_offer("vast", 1.0), _offer("vast", 1.1), _offer("runpod", 2.0), _offer("lambda", 3.0)]

    placement = place_with_failover(ranked, _SPEC, adapters, db_session, job_id, max_attempts=3)

    assert placement.offer.provider == "lambda"
    assert adapters["vast"].submit.call_count == 1  # one attempt per provider
    assert _events(db_session) == [("vast", "runpod"), ("runpod", "lambda")]
    reasons = [e.reason for e in db_session.query(FailoverEvent).order_by(FailoverEvent.id)]
    assert reasons == ["vast down", "runpod full"]


def test_stops_at_max_attempts(db_session, job_id):
    adapters = {"vast": _failing(), "runpod": _failing(), "lambda": _ok("l-1")}
    ranked = [_offer("vast", 1.0), _offer("runpod", 2.0), _offer("lambda", 3.0)]

    with pytest.raises(ProviderError, match="after 2 attempt"):
        place_with_failover(ranked, _SPEC, adapters, db_session, job_id, max_attempts=2)

    adapters["lambda"].submit.assert_not_called()
    assert _events(db_session) == [("vast", "runpod")]


def test_provider_without_adapter_is_skipped_without_using_an_attempt(db_session, job_id):
    adapters = {"runpod": _ok("r-1")}
    ranked = [_offer("vast", 1.0), _offer("runpod", 2.0)]

    placement = place_with_failover(ranked, _SPEC, adapters, db_session, job_id, max_attempts=1)

    assert placement.offer.provider == "runpod"
    assert _events(db_session) == []


def test_replacement_after_death_logs_failover_from_dead_provider(db_session, job_id):
    adapters = {"vast": _ok("v-2"), "runpod": _ok("r-1")}
    ranked = [_offer("vast", 1.0), _offer("runpod", 2.0)]

    placement = place_with_failover(
        ranked, _SPEC, adapters, db_session, job_id,
        max_attempts=2, exclude={"vast"}, failed_provider="vast", failure_reason="job died",
    )

    assert placement.offer.provider == "runpod"
    adapters["vast"].submit.assert_not_called()
    assert _events(db_session) == [("vast", "runpod")]


def test_raises_when_every_provider_is_excluded(db_session, job_id):
    with pytest.raises(ProviderError, match="No untried provider"):
        place_with_failover(
            [_offer("vast", 1.0)], _SPEC, {"vast": _ok("v-1")}, db_session, job_id,
            max_attempts=3, exclude={"vast"}, failed_provider="vast", failure_reason="job died",
        )
    assert _events(db_session) == []
