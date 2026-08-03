import logging
from typing import Any

import httpx

logger = logging.getLogger("model_router")


class UpstreamUnavailableError(Exception):
    """Raised when a downstream serving instance can't be reached at all."""

    def __init__(self, version: str, base_url: str, reason: str) -> None:
        self.version = version
        self.base_url = base_url
        self.reason = reason
        super().__init__(f"{version} ({base_url}) unreachable: {reason}")


async def check_ready(client: httpx.AsyncClient, base_url: str, timeout: float) -> bool:
    try:
        response = await client.get(f"{base_url}/ready", timeout=timeout)
    except httpx.RequestError as exc:
        logger.warning("readiness check failed for %s: %s", base_url, exc)
        return False
    return response.status_code == 200


async def forward_predict(
    client: httpx.AsyncClient,
    version: str,
    base_url: str,
    payload: dict[str, Any],
    timeout: float,
) -> httpx.Response:
    """Forward the request body as-is. The router does not validate it against the
    target's /predict schema - each version owns its own schema, and the router isn't
    in a position to know it without an extra round-trip; downstream is the one that
    validates and returns 422 on a bad shape.
    """
    try:
        return await client.post(f"{base_url}/predict", json=payload, timeout=timeout)
    except httpx.RequestError as exc:
        raise UpstreamUnavailableError(version, base_url, str(exc)) from exc
