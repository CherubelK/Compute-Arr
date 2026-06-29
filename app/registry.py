"""
Builds the live provider adapter registry from config.
An adapter is only instantiated when its API key is non-empty.
"""
from app.config import settings
from app.providers.base import ProviderAdapter
from app.providers.lambda_labs import LambdaAdapter
from app.providers.runpod import RunPodAdapter
from app.providers.vast import VastAdapter


def build_registry() -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}

    if settings.runpod_api_key:
        adapters["runpod"] = RunPodAdapter(api_key=settings.runpod_api_key)

    if settings.vast_api_key:
        adapters["vast"] = VastAdapter(api_key=settings.vast_api_key)

    if settings.lambda_api_key:
        adapters["lambda"] = LambdaAdapter(
            api_key=settings.lambda_api_key,
            ssh_key_names=settings.lambda_ssh_key_names,
        )

    return adapters
