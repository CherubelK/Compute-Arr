"""Smoke tests for SQLAlchemy model definitions (no DB required)."""
import uuid

import pytest

from app.models import FailoverEvent, Job, PriceSnapshot, Provider, RoutingDecision


def test_provider_construction():
    p = Provider(id="runpod", display_name="RunPod", enabled=True, healthy=True)
    assert p.id == "runpod"
    assert p.enabled is True
    assert p.healthy is True


def test_price_snapshot_fields():
    snap = PriceSnapshot(provider_id="runpod", gpu_class="H100", price_per_hr=2.49, available=True)
    assert snap.gpu_class == "H100"
    assert float(snap.price_per_hr) == 2.49


def test_job_construction():
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        gpu_class="H100",
        preference="cheapest",
        image="pytorch/pytorch:latest",
        baseline_provider="lambda",
        status="pending",
    )
    assert job.id == job_id
    assert job.status == "pending"
    assert job.actual_cost is None


def test_routing_decision_fields():
    job_id = uuid.uuid4()
    rd = RoutingDecision(
        job_id=job_id,
        ranked_offers=[{"provider": "runpod", "price_per_hr": 2.49}],
        chosen_provider="runpod",
        reason="cheapest available",
    )
    assert rd.chosen_provider == "runpod"
    assert rd.ranked_offers[0]["provider"] == "runpod"


def test_failover_event_fields():
    job_id = uuid.uuid4()
    fe = FailoverEvent(
        job_id=job_id,
        from_provider="runpod",
        to_provider="vast",
        reason="placement failed",
    )
    assert fe.from_provider == "runpod"
    assert fe.to_provider == "vast"
