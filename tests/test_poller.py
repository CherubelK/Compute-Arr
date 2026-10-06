"""
Tests for the poller. Uses SQLite in-memory for the DB and a stub adapter.
No real provider API calls; no Postgres required.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cache import OfferCache
from app.database import Base
from app.models import PriceSnapshot
from app.poller import poll_once
from app.providers.base import Offer, ProviderAdapter, ProviderError


# ---------------------------------------------------------------------------
# SQLite in-memory test DB
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


# ---------------------------------------------------------------------------
# Stub adapters
# ---------------------------------------------------------------------------

class GoodAdapter(ProviderAdapter):
    def get_offers(self, gpu_class: str) -> list[Offer]:
        return [Offer(provider="stub", gpu_class=gpu_class, price_per_hr=1.99, available=True)]

    def submit(self, job_spec): ...
    def status(self, provider_job_id): ...
    def cancel(self, provider_job_id): ...


class FailingAdapter(ProviderAdapter):
    def get_offers(self, gpu_class: str) -> list[Offer]:
        raise ProviderError("connection refused")

    def submit(self, job_spec): ...
    def status(self, provider_job_id): ...
    def cancel(self, provider_job_id): ...


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_poll_once_populates_cache(db_session, monkeypatch):
    monkeypatch.setattr("app.poller.SessionLocal", lambda: db_session)

    cache = OfferCache()
    poll_once({"stub": GoodAdapter()}, cache, ["H100", "A100"])

    snapshots = cache.get_all()
    assert len(snapshots) == 1
    assert snapshots[0].provider_id == "stub"
    assert snapshots[0].healthy is True
    assert len(snapshots[0].offers) == 2  # one per gpu_class


def test_poll_once_writes_price_snapshots(db_session, monkeypatch):
    monkeypatch.setattr("app.poller.SessionLocal", lambda: db_session)

    cache = OfferCache()
    poll_once({"stub": GoodAdapter()}, cache, ["H100"])

    rows = db_session.query(PriceSnapshot).all()
    assert len(rows) == 1
    assert rows[0].provider_id == "stub"
    assert rows[0].gpu_class == "H100"
    assert float(rows[0].price_per_hr) == pytest.approx(1.99)


def test_poll_once_marks_unhealthy_on_provider_error(db_session, monkeypatch):
    monkeypatch.setattr("app.poller.SessionLocal", lambda: db_session)

    cache = OfferCache()
    poll_once({"bad": FailingAdapter()}, cache, ["H100"])

    snapshots = cache.get_all()
    assert snapshots[0].healthy is False
    assert len(snapshots[0].offers) == 0


def test_poll_once_appends_not_overwrites(db_session, monkeypatch):
    monkeypatch.setattr("app.poller.SessionLocal", lambda: db_session)

    cache = OfferCache()
    poll_once({"stub": GoodAdapter()}, cache, ["H100"])
    poll_once({"stub": GoodAdapter()}, cache, ["H100"])

    rows = db_session.query(PriceSnapshot).all()
    assert len(rows) == 2  # two separate snapshots, not overwritten


def test_cache_get_offers_for_gpu_filters_by_class(db_session, monkeypatch):
    monkeypatch.setattr("app.poller.SessionLocal", lambda: db_session)

    cache = OfferCache()
    poll_once({"stub": GoodAdapter()}, cache, ["H100", "A100"])

    h100_offers = cache.get_offers_for_gpu("H100")
    assert all(o.gpu_class == "H100" for o in h100_offers)
    assert len(h100_offers) == 1


def test_cache_excludes_unhealthy_providers(db_session, monkeypatch):
    monkeypatch.setattr("app.poller.SessionLocal", lambda: db_session)

    cache = OfferCache()
    poll_once({"bad": FailingAdapter()}, cache, ["H100"])

    offers = cache.get_offers_for_gpu("H100")
    assert offers == []
