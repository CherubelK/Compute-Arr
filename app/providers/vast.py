"""
Vast.ai adapter.

API base: https://console.vast.ai/api/v0/
Auth: ?api_key={key} query param on every request.

GPU class map: short names → Vast.ai gpu_name strings (matched with 'in' for flexibility).
"""
import json
from typing import Optional

import httpx

from app.providers.base import JobSpec, JobStatus, Offer, ProviderAdapter, ProviderError

_BASE = "https://console.vast.ai/api/v0"

# Maps our GPU class → substring(s) present in Vast.ai's gpu_name field.
_GPU_CLASS_MAP: dict[str, list[str]] = {
    "H100": ["H100"],
    "A100": ["A100"],
    "A40": ["A40"],
    "RTX4090": ["4090"],
    "RTX3090": ["3090"],
    "RTX3080": ["3080"],
}

_STATUS_MAP: dict[str, str] = {
    "running": "running",
    "loading": "pending",
    "offline": "failed",
    "exited": "succeeded",
    "destroyed": "cancelled",
}


def _match_gpu(gpu_name: str, gpu_class: str) -> bool:
    needles = _GPU_CLASS_MAP.get(gpu_class.upper(), [])
    return any(n.lower() in gpu_name.lower() for n in needles)


class VastAdapter(ProviderAdapter):
    PROVIDER_ID = "vast"

    def __init__(self, api_key: str, http_client: Optional[httpx.Client] = None):
        self._api_key = api_key
        self._client = http_client or httpx.Client(timeout=30)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        p = {"api_key": self._api_key, **(params or {})}
        resp = self._client.get(f"{_BASE}{path}", params=p)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"Vast.ai HTTP {exc.response.status_code}: {exc.response.text}") from exc
        return resp.json()

    def _put(self, path: str, body: dict) -> dict:
        resp = self._client.put(f"{_BASE}{path}", params={"api_key": self._api_key}, json=body)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"Vast.ai HTTP {exc.response.status_code}: {exc.response.text}") from exc
        return resp.json()

    def _delete(self, path: str) -> dict:
        resp = self._client.delete(f"{_BASE}{path}", params={"api_key": self._api_key})
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"Vast.ai HTTP {exc.response.status_code}: {exc.response.text}") from exc
        return resp.json()

    # ------------------------------------------------------------------

    def get_offers(self, gpu_class: str) -> list[Offer]:
        if gpu_class.upper() not in _GPU_CLASS_MAP:
            raise ProviderError(f"Unknown GPU class for Vast.ai: {gpu_class!r}")

        query = json.dumps({"rentable": {"eq": True}, "num_gpus": {"eq": 1}})
        data = self._get("/bundles/", params={"q": query})

        offers: list[Offer] = []
        for item in data.get("offers", []):
            if not _match_gpu(item.get("gpu_name", ""), gpu_class):
                continue
            price = item.get("dph_total")
            if price is None:
                continue
            offers.append(
                Offer(
                    provider=self.PROVIDER_ID,
                    gpu_class=gpu_class,
                    price_per_hr=float(price),
                    available=item.get("rentable", False),
                    region=item.get("geolocation"),
                    raw_offer_id=str(item["id"]),
                )
            )
        return offers

    def submit(self, job_spec: JobSpec) -> str:
        if job_spec.gpu_class.upper() not in _GPU_CLASS_MAP:
            raise ProviderError(f"Unknown GPU class for Vast.ai: {job_spec.gpu_class!r}")

        # Search for the cheapest matching rentable offer
        query = json.dumps({"rentable": {"eq": True}, "num_gpus": {"eq": 1}})
        data = self._get("/bundles/", params={"q": query})

        candidates = [
            item for item in data.get("offers", [])
            if _match_gpu(item.get("gpu_name", ""), job_spec.gpu_class)
            and item.get("rentable")
        ]
        if not candidates:
            raise ProviderError(f"No rentable Vast.ai offers for {job_spec.gpu_class}")

        best = min(candidates, key=lambda x: x.get("dph_total", float("inf")))
        offer_id = best["id"]

        env_str = " ".join(f"-e {k}={v}" for k, v in (job_spec.env_vars or {}).items())
        result = self._put(
            f"/asks/{offer_id}/",
            {
                "client_id": "me",
                "image": job_spec.image,
                "env": env_str,
                "onstart": job_spec.command or "",
                "runtype": "args",
                "args_str": "",
            },
        )
        if not result.get("success"):
            raise ProviderError(f"Vast.ai rent failed: {result}")

        return str(result["new_contract"])

    def status(self, provider_job_id: str) -> JobStatus:
        data = self._get(f"/instances/{provider_job_id}/")
        instances = data.get("instances", [])
        if not instances:
            raise ProviderError(f"Vast.ai instance not found: {provider_job_id}")

        inst = instances[0]
        raw = inst.get("actual_status") or "loading"  # null until the instance starts loading
        state = _STATUS_MAP.get(raw, "failed")
        cost_per_hr: Optional[float] = inst.get("cost_per_hr")
        duration_s: Optional[float] = inst.get("duration")
        cost_so_far = None
        if cost_per_hr is not None and duration_s is not None:
            cost_so_far = round(cost_per_hr * duration_s / 3600, 6)

        return JobStatus(
            provider_job_id=provider_job_id,
            state=state,
            cost_so_far=cost_so_far,
        )

    def cancel(self, provider_job_id: str) -> None:
        self._delete(f"/instances/{provider_job_id}/")

    def logs(self, provider_job_id: str) -> str:
        data = self._get(f"/instances/{provider_job_id}/")
        instances = data.get("instances", [])
        if not instances:
            return ""
        return instances[0].get("logs") or ""
