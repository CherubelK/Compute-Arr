from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.database import get_db
from app.models import Job, RoutingDecision

router = APIRouter()


@router.get("/usage")
def usage(
    db: Session = Depends(get_db),
    _: None = Depends(require_api_key),
):
    jobs = db.query(Job).all()

    # Every job that ran cost money, whether or not it finished cleanly.
    total_cost = sum(float(j.actual_cost or 0) for j in jobs)

    by_status: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    for j in jobs:
        by_status[j.status] = by_status.get(j.status, 0) + 1
        if j.chosen_provider:
            by_provider[j.chosen_provider] = by_provider.get(j.chosen_provider, 0) + 1

    estimated_savings = _compute_savings(jobs, db)
    baseline_cost = total_cost + estimated_savings
    savings_pct = (estimated_savings / baseline_cost * 100) if baseline_cost > 0 else 0.0

    return {
        "total_jobs": len(jobs),
        "completed_jobs": by_status.get("succeeded", 0),
        "failed_jobs": by_status.get("failed", 0),
        "total_cost_usd": round(total_cost, 4),
        "by_provider": by_provider,
        "estimated_savings_usd": round(estimated_savings, 4),
        "savings_pct": round(savings_pct, 2),
    }


def _compute_savings(jobs: list[Job], db: Session) -> float:
    """
    Estimate savings vs. the baseline provider across all jobs with a cost.

    For each job we take the hourly price it was placed at and the baseline
    provider's best price in the routing decision's ranked_offers (i.e. at
    the moment the job was routed), and compute:
        savings = (baseline_price - placed_price) * duration_hours
    where duration_hours is back-calculated from actual_cost / placed_price.
    Jobs the baseline had no offer for are skipped.
    """
    decisions = {rd.job_id: rd for rd in db.query(RoutingDecision).all()}

    total = 0.0
    for job in jobs:
        if not job.actual_cost or not job.price_per_hr:
            continue

        rd = decisions.get(job.id)
        if not rd:
            continue

        baseline_prices = [
            o["price_per_hr"] for o in rd.ranked_offers if o["provider"] == job.baseline_provider
        ]
        if not baseline_prices:
            continue

        duration_hours = float(job.actual_cost) / float(job.price_per_hr)
        total += min(baseline_prices) * duration_hours - float(job.actual_cost)

    return total
