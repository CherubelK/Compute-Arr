from fastapi import Request

from app.providers.base import ProviderAdapter


def get_adapters(request: Request) -> dict[str, ProviderAdapter]:
    """FastAPI dependency — returns the live adapter registry stored on app.state."""
    return getattr(request.app.state, "registry", {})
