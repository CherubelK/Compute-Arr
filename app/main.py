from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.providers import router as providers_router
from app.api.usage import router as usage_router
from app.registry import build_registry
from app.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    adapters = build_registry()
    app.state.registry = adapters
    scheduler = start_scheduler(adapters) if adapters else None
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Compute-Arr GPU Router", version="0.1.0", lifespan=lifespan)

app.include_router(health_router)
app.include_router(providers_router)
app.include_router(jobs_router)
app.include_router(usage_router)
