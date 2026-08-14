import logging

import httpx

logger = logging.getLogger("model_router")


async def send_metric(
    client: httpx.AsyncClient,
    control_plane_url: str,
    deployment_id: str,
    model_version: str,
    latency_ms: float,
    status_code: int,
    prediction: int | None,
    prediction_id: str | None,
    timeout: float,
) -> None:
    """POST one metric row to the control plane. Always run as a background task
    (see app/router/main.py's route_predict) - never awaited inline, and any failure
    here is caught and logged, never allowed to propagate.
    """
    url = f"{control_plane_url.rstrip('/')}/api/deployments/{deployment_id}/metrics"
    payload = {
        "model_version": model_version,
        "latency_ms": latency_ms,
        "status_code": status_code,
        "prediction": prediction,
        "prediction_id": prediction_id,
    }
    try:
        await client.post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        logger.warning("metric push failed for deployment %s: %s", deployment_id, exc)
