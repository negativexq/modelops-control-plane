import logging
from typing import Any

import httpx
from fastapi import Request

from app.control_plane.config import control_plane_settings

logger = logging.getLogger("control_plane")


class RouterUpdateError(Exception):
    """Raised when the router rejects or can't be reached for a config push."""


class RouterGateway:
    """Thin client the control plane uses to push traffic allocations to the router.

    The control plane only ever sends {model_name, deployment_id,
    targets: [{version, weight}, ...]} - it never sends host/port. Resolving a version
    to a host:port is the router's own deployment concern (see
    app/router/config.py's version_hosts map), not the control plane's.

    Takes a shared httpx.AsyncClient rather than opening/closing one per call - the
    client is created once in app.main's lifespan and reused across requests.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, timeout: float) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, targets: list[dict[str, Any]]
    ) -> None:
        url = f"{self._base_url}/router/config"
        body = {"model_name": model_name, "deployment_id": deployment_id, "targets": targets}
        try:
            response = await self._client.put(url, json=body, timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("router config push failed: %s", exc)
            raise RouterUpdateError(str(exc)) from exc


def get_router_gateway(request: Request) -> RouterGateway:
    return RouterGateway(
        request.app.state.http_client,
        control_plane_settings.router_base_url,
        control_plane_settings.router_timeout_seconds,
    )
