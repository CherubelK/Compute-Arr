"""
RunPod adapter.

Uses RunPod's GraphQL API (https://api.runpod.io/graphql).
All auth via RUNPOD_API_KEY env var — never hardcoded.

GPU class mapping: we accept short names like "H100", "A100", "RTX3090" and
map them to the RunPod GPU type IDs used in the API.
"""
from typing import Optional

import httpx

from app.providers.base import JobSpec, JobStatus, Offer, ProviderAdapter, ProviderError

_GRAPHQL_URL = "https://api.runpod.io/graphql"

# Map our normalized GPU class names → RunPod gpuTypeId values.
# Extend as needed; keys are matched case-insensitively.
_GPU_CLASS_MAP: dict[str, str] = {
    "H100": "NVIDIA H100 SXM",
    "H100_PCIE": "NVIDIA H100 PCIe",
    "A100": "NVIDIA A100-SXM4-80GB",
    "A100_PCIE": "NVIDIA A100 PCIe",
    "A40": "NVIDIA A40",
    "RTX4090": "NVIDIA GeForce RTX 4090",
    "RTX3090": "NVIDIA GeForce RTX 3090",
    "RTX3080": "NVIDIA GeForce RTX 3080",
}

# RunPod pod status → our normalized state
_STATUS_MAP: dict[str, str] = {
    "CREATED": "pending",
    "RUNNING": "running",
    "PAUSED": "running",
    "EXITED": "succeeded",
    "DEAD": "failed",
    "TERMINATED": "cancelled",
}


def _runpod_gpu_id(gpu_class: str) -> str:
    key = gpu_class.upper()
    if key not in _GPU_CLASS_MAP:
        raise ProviderError(f"Unknown GPU class for RunPod: {gpu_class!r}")
    return _GPU_CLASS_MAP[key]


class RunPodAdapter(ProviderAdapter):
    PROVIDER_ID = "runpod"

    def __init__(self, api_key: str, http_client: Optional[httpx.Client] = None):
        self._api_key = api_key
        self._client = http_client or httpx.Client(timeout=30)

    def _gql(self, query: str, variables: Optional[dict] = None) -> dict:
        resp = self._client.post(
            _GRAPHQL_URL,
            params={"api_key": self._api_key},
            json={"query": query, "variables": variables or {}},
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"RunPod HTTP {exc.response.status_code}: {exc.response.text}") from exc

        payload = resp.json()
        if "errors" in payload:
            raise ProviderError(f"RunPod GraphQL error: {payload['errors']}")
        return payload["data"]

    # ------------------------------------------------------------------
    # ProviderAdapter interface
    # ------------------------------------------------------------------

    def get_offers(self, gpu_class: str) -> list[Offer]:
        gpu_type_id = _runpod_gpu_id(gpu_class)

        query = """
        query GpuTypes {
          gpuTypes {
            id
            displayName
            lowestPrice(input: { gpuCount: 1 }) {
              minimumBidPrice
              uninterruptablePrice
            }
          }
        }
        """
        data = self._gql(query)
        offers: list[Offer] = []

        for gpu in data.get("gpuTypes", []):
            if gpu["id"] != gpu_type_id:
                continue
            prices = gpu.get("lowestPrice") or {}
            price = prices.get("uninterruptablePrice") or prices.get("minimumBidPrice")
            if price is None:
                continue
            offers.append(
                Offer(
                    provider=self.PROVIDER_ID,
                    gpu_class=gpu_class,
                    price_per_hr=float(price),
                    available=True,
                    region=None,
                    raw_offer_id=gpu["id"],
                )
            )

        return offers

    def submit(self, job_spec: JobSpec) -> str:
        gpu_type_id = _runpod_gpu_id(job_spec.gpu_class)

        env = [
            {"key": k, "value": v}
            for k, v in (job_spec.env_vars or {}).items()
        ]

        mutation = """
        mutation PodDeploy($input: PodFindAndDeployOnDemandInput!) {
          podFindAndDeployOnDemand(input: $input) {
            id
          }
        }
        """
        variables = {
            "input": {
                "gpuTypeId": gpu_type_id,
                "gpuCount": 1,
                "imageName": job_spec.image,
                "containerDiskInGb": 20,
                "volumeInGb": 0,
                "startJupyter": False,
                "startSsh": False,
                "dockerArgs": job_spec.command or "",
                "env": env,
                "cloudType": "ALL",
            }
        }
        data = self._gql(mutation, variables)
        pod_id: str = data["podFindAndDeployOnDemand"]["id"]
        return pod_id

    def status(self, provider_job_id: str) -> JobStatus:
        query = """
        query PodStatus($podId: String!) {
          pod(input: { podId: $podId }) {
            id
            desiredStatus
            costPerHr
            runtime { uptimeInSeconds }
          }
        }
        """
        data = self._gql(query, {"podId": provider_job_id})
        pod = data.get("pod")
        if pod is None:
            raise ProviderError(f"RunPod pod not found: {provider_job_id}")

        raw_status = pod.get("desiredStatus", "DEAD")
        state = _STATUS_MAP.get(raw_status, "failed")
        cost_per_hr: Optional[float] = pod.get("costPerHr")
        uptime_s: Optional[int] = (pod.get("runtime") or {}).get("uptimeInSeconds")
        cost_so_far = None
        if cost_per_hr is not None and uptime_s is not None:
            cost_so_far = round(cost_per_hr * uptime_s / 3600, 6)

        return JobStatus(
            provider_job_id=provider_job_id,
            state=state,
            cost_so_far=cost_so_far,
        )

    def cancel(self, provider_job_id: str) -> None:
        mutation = """
        mutation PodStop($podId: String!) {
          podStop(input: { podId: $podId }) { id desiredStatus }
        }
        """
        self._gql(mutation, {"podId": provider_job_id})

    def logs(self, provider_job_id: str) -> str:
        query = """
        query PodLogs($podId: String!) {
          pod(input: { podId: $podId }) { logs }
        }
        """
        data = self._gql(query, {"podId": provider_job_id})
        pod = data.get("pod") or {}
        return pod.get("logs") or ""
