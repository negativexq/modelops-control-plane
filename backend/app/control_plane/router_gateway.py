import logging
from typing import Any

import httpx
from fastapi import Request

from app.control_plane.config import control_plane_settings

logger = logging.getLogger("control_plane")


class RouterUpdateError(Exception):
    """Raised when the router rejects (for a reason other than a stale revision -
    see StaleRevisionError) or can't be reached for a config push."""


class StaleRevisionError(Exception):
    """Raised when the router rejects a push because it already has an
    equal-or-newer revision applied for this exact deployment_id (HTTP 409 - see
    app/router/main.py's put_config). Not a failure: it means either a losing
    side of a concurrent write (someone else's newer push already landed) or the
    reconciler re-pushing data the router turned out to already have. Callers
    must not treat this like RouterUpdateError - in particular, never transition
    a deployment to FAILED because of it. See
    docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    """


class RouterGateway:
    """Thin client the control plane uses to push traffic allocations to the router,
    and to read back what the router currently reports it has applied (observed
    state) for reconciliation.

    The control plane only ever sends {model_name, deployment_id, revision,
    targets: [{version, weight}, ...]} - it never sends host/port. Resolving a
    version to a host:port is the router's own deployment concern (see
    app/router/config.py's version_hosts map), not the control plane's.

    Takes a shared httpx.AsyncClient rather than opening/closing one per call - the
    client is created once in app.main's lifespan and reused across requests.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, timeout: float) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, revision: int, targets: list[dict[str, Any]]
    ) -> None:
        url = f"{self._base_url}/router/config"
        body = {
            "model_name": model_name,
            "deployment_id": deployment_id,
            "revision": revision,
            "targets": targets,
        }
        try:
            response = await self._client.put(url, json=body, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("router config push failed: %s", exc)
            raise RouterUpdateError(str(exc)) from exc

        if response.status_code == 409:
            logger.info(
                "router rejected stale revision %s for deployment %s: %s",
                revision,
                deployment_id,
                response.text,
            )
            raise StaleRevisionError(response.text)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("router config push failed: %s", exc)
            raise RouterUpdateError(str(exc)) from exc

    async def get_observed_config(self) -> dict[str, Any] | None:
        """GETs the router's current observed config - {model_name, deployment_id,
        revision, targets}. Returns None if the router is unreachable: a router
        that's down has nothing to reconcile against right now, and the next
        reconcile tick will simply try again - see
        app/control_plane/reconcile.py.
        """
        url = f"{self._base_url}/router/config"
        try:
            response = await self._client.get(url, timeout=self._timeout)
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        except httpx.HTTPError as exc:
            logger.warning("router config fetch failed during reconcile: %s", exc)
            return None


def get_router_gateway(request: Request) -> RouterGateway:
    return RouterGateway(
        request.app.state.http_client,
        control_plane_settings.router_base_url,
        control_plane_settings.router_timeout_seconds,
    )
