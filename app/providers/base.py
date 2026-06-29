"""
Shared data types and abstract interface for all provider adapters.

Every adapter receives a JobSpec and returns Offer / JobStatus objects.
The router and failover logic only ever see these normalized types.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Offer:
    provider: str
    gpu_class: str
    price_per_hr: float
    available: bool
    region: Optional[str] = None
    raw_offer_id: Optional[str] = None


@dataclass
class JobSpec:
    gpu_class: str
    preference: str          # cheapest | fastest | reliable
    image: str
    command: Optional[str] = None
    env_vars: Optional[dict] = field(default_factory=dict)


@dataclass
class JobStatus:
    provider_job_id: str
    state: str               # pending | running | succeeded | failed | cancelled
    cost_so_far: Optional[float] = None


class ProviderError(Exception):
    """Raised when a provider API call fails in a way the router should handle."""


class ProviderAdapter(ABC):
    """Contract every provider adapter must satisfy."""

    @abstractmethod
    def get_offers(self, gpu_class: str) -> list[Offer]:
        """Return current available offers for the requested GPU class."""

    @abstractmethod
    def submit(self, job_spec: JobSpec) -> str:
        """Place a job. Returns the provider-assigned job ID."""

    @abstractmethod
    def status(self, provider_job_id: str) -> JobStatus:
        """Return current status + running cost for a job."""

    @abstractmethod
    def cancel(self, provider_job_id: str) -> None:
        """Cancel/stop a running job."""

    def logs(self, provider_job_id: str) -> str:
        """Return recent logs. Optional — adapters may leave this as empty string."""
        return ""
