from typing import Any, Protocol

import httpx


class ActionConflictError(Exception):
    """The control plane rejected an action (HTTP 409) because the deployment's
    state already moved - a human raced in with a manual promote/rollback, or the
    canary was already at its final stage. Expected, not a bug: the caller should
    just skip this deployment for the current cycle."""


class WorkerClient(Protocol):
    """What the worker loop (app/worker/loop.py) needs. A Protocol rather than a
    concrete base class so tests can supply a pure in-memory fake without needing an
    HTTP server (see tests/test_worker_loop.py)."""

    async def list_deployments(self) -> list[dict[str, Any]]: ...
    async def get_deployment(self, deployment_id: str) -> dict[str, Any]: ...
    async def get_policy_evaluations(self, deployment_id: str) -> list[dict[str, Any]]: ...
    async def evaluate(self, deployment_id: str) -> dict[str, Any]: ...
    async def advance_traffic(self, deployment_id: str) -> dict[str, Any]: ...
    async def promote(self, deployment_id: str) -> dict[str, Any]: ...
    async def rollback(self, deployment_id: str) -> dict[str, Any]: ...
    async def record_inconclusive(self, deployment_id: str) -> dict[str, Any]: ...


class HttpWorkerClient:
    """Talks to the control plane's own REST API - the same endpoints a human/
    dashboard would call, with `triggered_by=automatic` on promote/rollback so the
    audit trail can tell the two apart."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, timeout: float) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _get(self, path: str) -> Any:
        response = await self._client.get(f"{self._base_url}{path}", timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        response = await self._client.post(url, timeout=self._timeout, **kwargs)
        if response.status_code == 409:
            raise ActionConflictError(response.text)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def list_deployments(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._get("/api/deployments")
        return result

    async def get_deployment(self, deployment_id: str) -> dict[str, Any]:
        return await self._get(f"/api/deployments/{deployment_id}")  # type: ignore[no-any-return]

    async def get_policy_evaluations(self, deployment_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._get(
            f"/api/deployments/{deployment_id}/policy-evaluations"
        )
        return result

    async def evaluate(self, deployment_id: str) -> dict[str, Any]:
        # No body - lets the endpoint fall back to the deployment's own persisted
        # policy_config instead of an env-default PolicyConfig(**{}).
        return await self._post(f"/api/deployments/{deployment_id}/evaluate")

    async def advance_traffic(self, deployment_id: str) -> dict[str, Any]:
        return await self._post(f"/api/deployments/{deployment_id}/advance-traffic")

    async def promote(self, deployment_id: str) -> dict[str, Any]:
        return await self._post(
            f"/api/deployments/{deployment_id}/promote", params={"triggered_by": "automatic"}
        )

    async def rollback(self, deployment_id: str) -> dict[str, Any]:
        return await self._post(
            f"/api/deployments/{deployment_id}/rollback", params={"triggered_by": "automatic"}
        )

    async def record_inconclusive(self, deployment_id: str) -> dict[str, Any]:
        return await self._post(f"/api/deployments/{deployment_id}/record-inconclusive")
