import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.worker.client import ActionConflictError, WorkerClient
from app.worker.decide import WorkerAction, decide_action

logger = logging.getLogger("worker")

ACTIVE_STATUSES = {"CANARY_RUNNING", "EVALUATING"}


def _canary_at_full_traffic(deployment: dict[str, Any]) -> bool:
    allocation = deployment.get("traffic_allocation")
    if not allocation:
        return False
    canary_version = deployment["canary_version"]
    for target in allocation["targets"]:
        if target["version"] == canary_version:
            return bool(target["weight"] >= 1.0 - 1e-9)
    return False


def _evaluation_window_seconds(deployment: dict[str, Any], default: int) -> int:
    policy_config = deployment.get("policy_config")
    if policy_config:
        return int(policy_config.get("evaluation_window_seconds", default))
    return default


async def _is_due_for_evaluation(
    client: WorkerClient, deployment: dict[str, Any], default_window_seconds: int
) -> bool:
    """No in-memory "last checked" state - due-ness is derived from the most recent
    PolicyEvaluation row on record, so this is correct immediately after a worker
    restart with zero warm-up."""
    evaluations = await client.get_policy_evaluations(deployment["id"])
    if not evaluations:
        return True

    last_evaluated_at = datetime.fromisoformat(evaluations[0]["evaluated_at"])
    if last_evaluated_at.tzinfo is None:
        # SQLite doesn't actually persist tzinfo (DateTime(timezone=True) isn't
        # enforced by the driver), so a value written as UTC can round-trip through
        # the API as a naive timestamp. Everything the control plane writes is UTC
        # (see control_plane/models.py's _utcnow), so that's a safe assumption here.
        last_evaluated_at = last_evaluated_at.replace(tzinfo=UTC)
    window_seconds = _evaluation_window_seconds(deployment, default_window_seconds)
    elapsed = (datetime.now(UTC) - last_evaluated_at).total_seconds()
    return elapsed >= window_seconds


async def process_deployment(
    client: WorkerClient, deployment_id: str, default_window_seconds: int
) -> WorkerAction | None:
    """One deployment, one cycle. Returns the action taken (or None if skipped),
    mainly for tests to assert against.
    """
    deployment = await client.get_deployment(deployment_id)
    if deployment["status"] not in ACTIVE_STATUSES:
        # Raced away since the caller listed active deployments - a human or an
        # earlier cycle already moved it on. Nothing to do.
        return None

    if not await _is_due_for_evaluation(client, deployment, default_window_seconds):
        return None

    try:
        evaluation = await client.evaluate(deployment_id)
    except ActionConflictError:
        return None

    action = decide_action(evaluation["overall_result"], _canary_at_full_traffic(deployment))

    try:
        if action == WorkerAction.ADVANCE_TRAFFIC:
            await client.advance_traffic(deployment_id)
        elif action == WorkerAction.PROMOTE:
            await client.promote(deployment_id)
        elif action == WorkerAction.ROLLBACK:
            await client.rollback(deployment_id)
        elif action == WorkerAction.RECORD_INCONCLUSIVE:
            await client.record_inconclusive(deployment_id)
    except ActionConflictError:
        logger.info("skipping deployment %s: state changed concurrently", deployment_id)
        return None

    logger.info(
        "deployment %s: %s -> %s", deployment_id, evaluation["overall_result"], action.value
    )
    return action


async def run_once(client: WorkerClient, default_window_seconds: int) -> None:
    """One sweep over every currently-active deployment. Stateless: the active list
    is re-fetched from the control plane every time, so nothing here survives - or
    needs to survive - a restart."""
    deployments = await client.list_deployments()
    active_ids = [d["id"] for d in deployments if d["status"] in ACTIVE_STATUSES]

    for deployment_id in active_ids:
        try:
            await process_deployment(client, deployment_id, default_window_seconds)
        except Exception:
            logger.exception("error processing deployment %s", deployment_id)


async def run_forever(
    client: WorkerClient, poll_interval_seconds: float, default_window_seconds: int
) -> None:
    while True:
        try:
            await run_once(client, default_window_seconds)
        except Exception:
            # A transient control-plane outage (e.g. mid-restart/mid-migration)
            # must not kill the worker process - there's nothing in-memory to lose
            # (see run_once's docstring), so the only correct response is to log and
            # try again next cycle rather than exit and require an operator to
            # notice and restart the container.
            logger.exception("error during worker sweep - will retry next cycle")
        await asyncio.sleep(poll_interval_seconds)
