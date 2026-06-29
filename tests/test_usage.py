"""Tests for GET /usage."""
import uuid

import pytest

from app.models import Job, RoutingDecision


def test_usage_empty_db(client):
    resp = client.get("/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_jobs"] == 0
    assert data["total_cost_usd"] == 0.0
    assert data["estimated_savings_usd"] == 0.0


def test_usage_counts_jobs_and_cost(client, db_session):
    # Seed completed jobs directly into the test DB
    job_id = uuid.uuid4()
    db_session.add(Job(
        id=job_id,
        gpu_class="H100",
        preference="cheapest",
        image="pytorch/pytorch:latest",
        baseline_provider="lambda",
        chosen_provider="runpod",
        provider_job_id="pod-1",
        status="succeeded",
        actual_cost=2.49,
    ))
    db_session.add(RoutingDecision(
        job_id=job_id,
        ranked_offers=[
            {"provider": "runpod", "gpu_class": "H100", "price_per_hr": 2.49, "available": True},
            {"provider": "lambda", "gpu_class": "H100", "price_per_hr": 4.00, "available": True},
        ],
        chosen_provider="runpod",
        reason="cheapest",
    ))
    db_session.commit()

    resp = client.get("/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_jobs"] == 1
    assert data["completed_jobs"] == 1
    assert data["total_cost_usd"] == pytest.approx(2.49)
    assert data["by_provider"]["runpod"] == 1
    # savings = (4.00 - 2.49) * (2.49 / 2.49) = 1.51
    assert data["estimated_savings_usd"] == pytest.approx(1.51, rel=1e-2)
    assert data["savings_pct"] > 0


def test_usage_failed_jobs_not_counted_in_cost(client, db_session):
    db_session.add(Job(
        id=uuid.uuid4(),
        gpu_class="H100",
        preference="cheapest",
        image="pytorch/pytorch:latest",
        baseline_provider="lambda",
        chosen_provider="runpod",
        status="failed",
        actual_cost=None,
    ))
    db_session.commit()

    resp = client.get("/usage")
    data = resp.json()
    assert data["total_jobs"] == 1
    assert data["completed_jobs"] == 0
    assert data["total_cost_usd"] == 0.0
