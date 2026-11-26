import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from app.api.health import router as health_router
from app.api.models import router as models_router
from app.benchmarks import service as benchmark_service
from app.benchmarks.api import router as benchmarks_router
from app.config import settings
from app.control_plane.api import reconcile_router, router_config_router
from app.control_plane.api import router as deployments_router
from app.control_plane.labels_api import router as labels_router
from app.control_plane.live_api import router as live_router
from app.db import get_db
from app.observability import tracing
from app.observability.metrics import instrument_app
from app.policy.api import defaults_router as policy_defaults_router
from app.policy.api import router as policy_router

logger = logging.getLogger("control_plane")


def _fail_interrupted_benchmarks(app: FastAPI) -> None:
    # Through get_db (or a test's override of it), not SessionLocal directly, so
    # tests running the lifespan stay on their own database.
    sessions = app.dependency_overrides.get(get_db, get_db)()
    try:
        benchmark_service.fail_interrupted_runs(next(sessions))
    except SQLAlchemyError:
        logger.exception("could not check for interrupted benchmark runs")
    finally:
        sessions.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _fail_interrupted_benchmarks(app)
    # Shared client for RouterGateway (app/control_plane/router_gateway.py) - created
    # once here instead of per-request, since the control plane may push router config
    # updates frequently (every deployment/promote/rollback).
    app.state.http_client = httpx.AsyncClient()
    # Router pushes (PUT /router/config) join the trace of the request or
    # reconcile tick that caused them.
    tracing.instrument_httpx(app.state.http_client, tracing.configured_provider("control-plane"))
    yield
    await app.state.http_client.aclose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
instrument_app(app, "backend")
tracing.instrument_fastapi(app, tracing.configured_provider("control-plane"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_allow_origins.split(",") if origin],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(models_router)
app.include_router(deployments_router)
app.include_router(router_config_router)
app.include_router(reconcile_router)
app.include_router(labels_router)
app.include_router(policy_router)
app.include_router(policy_defaults_router)
app.include_router(benchmarks_router)
app.include_router(live_router)
