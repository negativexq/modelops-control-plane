from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.models import router as models_router
from app.benchmarks.api import router as benchmarks_router
from app.config import settings
from app.control_plane.api import router as deployments_router
from app.control_plane.api import router_config_router
from app.policy.api import router as policy_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Shared client for RouterGateway (app/control_plane/router_gateway.py) - created
    # once here instead of per-request, since the control plane may push router config
    # updates frequently (every deployment/promote/rollback).
    app.state.http_client = httpx.AsyncClient()
    yield
    await app.state.http_client.aclose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

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
app.include_router(policy_router)
app.include_router(benchmarks_router)
