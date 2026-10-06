"""
Lambda Labs adapter.

API base: https://cloud.lambdalabs.com/api/v1/
Auth: HTTP Basic with api_key as username, empty password.

GPU class map: instance type name substrings → our short GPU class names.
"""
from typing import Optional

import httpx

from app.providers.base import JobSpec, JobStatus, Offer, ProviderAdapter, ProviderError

_BASE = "https://cloud.lambdalabs.com/api/v1"

# Substrings in Lambda instance type names → our GPU class.
# Checked in order; first match wins.
_INSTANCE_GPU_MAP: list[tuple[str, str]] = [
    ("h100", "H100"),
    ("a100", "A100"),
    ("a40", "A40"),
    ("rtx4090", "RTX4090"),
    ("rtx3090", "RTX3090"),
    ("rtx3080", "RTX3080"),
]

_STATUS_MAP: dict[str, str] = {
    "booting": "pending",
    "active": "running",
    "unhealthy": "failed",
    "terminating": "cancelled",
    "terminated": "cancelled",
}


def _instance_type_to_gpu_class(name: str) -> Optional[str]:
    lower = name.lower()
    for substr, gpu_class in _INSTANCE_GPU_MAP:
        if substr in lower:
            return gpu_class
    return None


def _gpu_class_instance_types(instance_types: dict, gpu_class: str) -> list[tuple[str, dict]]:
    """Return (name, info) pairs for 1-GPU instances matching the GPU class."""
    result = []
    for name, info in instance_types.items():
        if not name.startswith("gpu_1x_"):
            continue
        if _instance_type_to_gpu_class(name) == gpu_class:
            result.append((name, info))
    return result


class LambdaAdapter(ProviderAdapter):
    PROVIDER_ID = "lambda"

    def __init__(
        self,
        api_key: str,
        ssh_key_names: Optional[list[str]] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        self._api_key = api_key
        self._ssh_key_names = ssh_key_names or []
        self._client = http_client or httpx.Client(
            timeout=30,
            auth=(api_key, ""),
        )

    def _get(self, path: str) -> dict:
        resp = self._client.get(f"{_BASE}{path}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"Lambda HTTP {exc.response.status_code}: {exc.response.text}") from exc
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        resp = self._client.post(f"{_BASE}{path}", json=body)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"Lambda HTTP {exc.response.status_code}: {exc.response.text}") from exc
        return resp.json()

    # ------------------------------------------------------------------

    def get_offers(self, gpu_class: str) -> list[Offer]:
        data = self._get("/instance-types")
        instance_types: dict = data.get("data", {})

        offers: list[Offer] = []
        for name, info in _gpu_class_instance_types(instance_types, gpu_class):
            itype = info.get("instance_type", {})
            price_cents = itype.get("price_cents_per_hour")
            if price_cents is None:
                continue
            regions = info.get("regions_with_capacity_available", [])
            available = len(regions) > 0
            region = regions[0]["name"] if regions else None
            offers.append(
                Offer(
                    provider=self.PROVIDER_ID,
                    gpu_class=gpu_class,
                    price_per_hr=price_cents / 100.0,
                    available=available,
                    region=region,
                    raw_offer_id=name,
                )
            )
        return offers

    def submit(self, job_spec: JobSpec) -> str:
        data = self._get("/instance-types")
        instance_types: dict = data.get("data", {})

        candidates = [
            (name, info)
            for name, info in _gpu_class_instance_types(instance_types, job_spec.gpu_class)
            if info.get("regions_with_capacity_available")
        ]
        if not candidates:
            raise ProviderError(f"No available Lambda instances for {job_spec.gpu_class}")

        # Pick cheapest available
        best_name, best_info = min(
            candidates,
            key=lambda x: x[1]["instance_type"].get("price_cents_per_hour", 999999),
        )
        regions = best_info["regions_with_capacity_available"]
        region = regions[0]["name"]

        # Build user-data script to run the command on startup
        user_data = None
        if job_spec.command:
            env_exports = "\n".join(
                f"export {k}={v}" for k, v in (job_spec.env_vars or {}).items()
            )
            user_data = f"#!/bin/bash\n{env_exports}\ndocker run {job_spec.image} {job_spec.command}"

        payload: dict = {
            "region_name": region,
            "instance_type_name": best_name,
            "ssh_key_names": self._ssh_key_names,
            "name": f"compute-arr-{job_spec.gpu_class.lower()}",
        }
        if user_data:
            payload["user_data"] = user_data

        result = self._post("/instance-operations/launch", payload)
        ids = result.get("data", {}).get("instance_ids", [])
        if not ids:
            raise ProviderError(f"Lambda launch returned no instance IDs: {result}")
        return ids[0]

    def status(self, provider_job_id: str) -> JobStatus:
        data = self._get(f"/instances/{provider_job_id}")
        inst = data.get("data")
        if inst is None:
            raise ProviderError(f"Lambda instance not found: {provider_job_id}")

        raw = inst.get("status", "unhealthy")
        state = _STATUS_MAP.get(raw, "failed")
        # Lambda doesn't expose running time, so cost can't be derived here.
        return JobStatus(
            provider_job_id=provider_job_id,
            state=state,
            cost_so_far=None,
        )

    def cancel(self, provider_job_id: str) -> None:
        self._post("/instance-operations/terminate", {"instance_ids": [provider_job_id]})

    def logs(self, provider_job_id: str) -> str:
        # Lambda Labs does not expose a logs API; return empty string
        return ""
