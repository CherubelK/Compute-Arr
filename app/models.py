import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "runpod"
    display_name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    price_snapshots: Mapped[list["PriceSnapshot"]] = relationship(back_populates="provider")


class PriceSnapshot(Base):
    """Append-only price/availability record. Never update rows — always insert."""

    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    gpu_class: Mapped[str] = mapped_column(String(64), index=True)
    price_per_hr: Mapped[float] = mapped_column(Numeric(10, 4))
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_offer_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    provider: Mapped["Provider"] = relationship(back_populates="price_snapshots")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gpu_class: Mapped[str] = mapped_column(String(64))
    preference: Mapped[str] = mapped_column(String(32))  # cheapest | fastest | reliable
    image: Mapped[str] = mapped_column(Text)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    env_vars: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    chosen_provider: Mapped[str | None] = mapped_column(String(64), ForeignKey("providers.id"), nullable=True)
    baseline_provider: Mapped[str] = mapped_column(String(64))
    provider_job_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|succeeded|failed|cancelled
    actual_cost: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    routing_decision: Mapped["RoutingDecision | None"] = relationship(back_populates="job", uselist=False)
    failover_events: Mapped[list["FailoverEvent"]] = relationship(back_populates="job")


class RoutingDecision(Base):
    """Snapshot of the ranked alternatives considered at job submission time."""

    __tablename__ = "routing_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"), unique=True, index=True)
    # list of {provider, price_per_hr, reason_score, ...} at decision time
    ranked_offers: Mapped[list[Any]] = mapped_column(JSON)
    chosen_provider: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped["Job"] = relationship(back_populates="routing_decision")


class FailoverEvent(Base):
    __tablename__ = "failover_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"), index=True)
    from_provider: Mapped[str] = mapped_column(String(64))
    to_provider: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped["Job"] = relationship(back_populates="failover_events")
