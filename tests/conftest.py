"""
Shared pytest fixtures for all test modules.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — must be imported before Base.metadata.create_all

from app.auth import require_api_key
from app.database import Base, get_db
from app.dependencies import get_adapters
from app.main import app


# ---------------------------------------------------------------------------
# In-memory SQLite DB (no Postgres needed for tests)
#
# StaticPool ensures all sessions reuse the same underlying connection, which
# is required for SQLite :memory: — otherwise each new connection sees a fresh
# empty database and the tables created by create_all() are invisible.
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)


@pytest.fixture()
def db_session(engine):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# FastAPI TestClient with dependency overrides
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(db_session):
    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_api_key] = lambda: None
    app.dependency_overrides[get_adapters] = lambda: {}

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def authed_client(client):
    """Client that sends the API key header (used when auth is NOT overridden)."""
    client.headers.update({"X-API-Key": "changeme"})
    return client
