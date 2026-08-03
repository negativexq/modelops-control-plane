"""A small synchronous client for the control plane's REST API, used only by the
benchmark orchestration scripts (backend/scripts/benchmarks/). Deliberately separate
from app/worker/client.py's async Protocol-based client - benchmarks are one-shot
scripts that poll on a timer and don't need worker's concurrency model, and creating
deployments (which the worker never does) is a first-class operation here.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx


class ControlPlaneClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ControlPlaneClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def create_deployment(
        self,
        model_name: str,
        stable_version: str,
        canary_version: str,
        canary_weight: float,
        policy_config: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_name": model_name,
            "stable_version": stable_version,
            "canary_version": canary_version,
            "canary_weight": canary_weight,
        }
        if policy_config is not None:
            payload["policy_config"] = policy_config
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        response = self._client.post("/api/deployments", json=payload, headers=headers)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_deployment(self, deployment_id: str) -> dict[str, Any]:
        response = self._client.get(f"/api/deployments/{deployment_id}")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_policy_evaluations(self, deployment_id: str) -> list[dict[str, Any]]:
        response = self._client.get(f"/api/deployments/{deployment_id}/policy-evaluations")
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        return result

    def post_metric(
        self,
        deployment_id: str,
        model_version: str,
        latency_ms: float,
        status_code: int = 200,
        prediction: int | None = None,
        actual_label: int | None = None,
    ) -> None:
        """Writes one PredictionMetric row directly - used only by the "success"
        scenario to simulate a delayed ground-truth label feed (see scenarios.py).
        The router itself never sets actual_label; there is no real label source in
        the platform yet (Sprint 5/7 notes)."""
        payload = {
            "model_version": model_version,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "prediction": prediction,
            "actual_label": actual_label,
        }
        response = self._client.post(f"/api/deployments/{deployment_id}/metrics", json=payload)
        response.raise_for_status()

    def rollback(self, deployment_id: str) -> dict[str, Any]:
        response = self._client.post(f"/api/deployments/{deployment_id}/rollback")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def promote(self, deployment_id: str) -> dict[str, Any]:
        response = self._client.post(f"/api/deployments/{deployment_id}/promote")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result
