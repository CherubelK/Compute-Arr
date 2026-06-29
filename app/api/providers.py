from datetime import datetime

from fastapi import APIRouter

from app.cache import offer_cache

router = APIRouter()


@router.get("/providers")
def list_providers() -> list[dict]:
    """Return the latest cached price/health snapshot for every known provider."""
    snapshots = offer_cache.get_all()
    return [
        {
            "provider": snap.provider_id,
            "healthy": snap.healthy,
            "polled_at": snap.polled_at,
            "offers": [
                {
                    "gpu_class": o.gpu_class,
                    "price_per_hr": o.price_per_hr,
                    "available": o.available,
                    "region": o.region,
                    "raw_offer_id": o.raw_offer_id,
                }
                for o in snap.offers
            ],
        }
        for snap in snapshots
    ]
