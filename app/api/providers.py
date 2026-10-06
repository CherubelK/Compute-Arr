from fastapi import APIRouter, Depends

from app.auth import require_api_key
from app.cache import offer_cache

router = APIRouter()


@router.get("/providers")
def list_providers(_: None = Depends(require_api_key)) -> list[dict]:
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
