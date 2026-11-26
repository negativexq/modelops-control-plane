"""Queues a notification when a rollout reaches a status a human should hear
about. Called from the control plane's state transition, inside its
transaction: the outbox row commits (or rolls back) together with the status
change, so a decision is never recorded without its notification, and a
notification is never sent for a decision that didn't commit."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.config import control_plane_settings
from app.control_plane.models import (
    Deployment,
    DeploymentStatus,
    PolicyEvaluation,
    PolicyEvaluationResult,
    WebhookDelivery,
)

# The statuses that end a rollout or stop it for a human. INCONCLUSIVE is the
# one that most needs a notification: it's the worker asking for someone.
NOTIFY_ON: dict[DeploymentStatus, str] = {
    DeploymentStatus.PROMOTED: "deployment.promoted",
    DeploymentStatus.ROLLED_BACK: "deployment.rolled_back",
    DeploymentStatus.INCONCLUSIVE: "deployment.frozen",
    DeploymentStatus.FAILED: "deployment.failed",
}

_HEADLINE = {
    DeploymentStatus.PROMOTED: "promoted to 100%",
    DeploymentStatus.ROLLED_BACK: "rolled back to {stable}",
    DeploymentStatus.INCONCLUSIVE: "frozen for a human: the evidence stayed inconclusive",
    DeploymentStatus.FAILED: "failed",
}


def _decided_by(target: DeploymentStatus, message: str) -> str:
    """Same rule as fleet.decision_actor: promote/rollback tag their message,
    and only the worker freezes a rollout."""
    if target == DeploymentStatus.INCONCLUSIVE or "(automatic)" in message:
        return "automatic"
    if "(manual)" in message:
        return "manual"
    return "unknown"


def _latest_checks(db: Session, deployment_id: str) -> list[dict[str, Any]]:
    """Each policy's most recent verdict, where it isn't PASS - the reason
    behind a rollback or a freeze, failures first. Empty for a clean promotion.
    (Rows of one evaluation each carry their own timestamp, so "latest per
    policy" rather than "the rows at the latest timestamp".)"""
    rows = db.scalars(
        select(PolicyEvaluation)
        .where(PolicyEvaluation.deployment_id == deployment_id)
        .order_by(PolicyEvaluation.evaluated_at.desc())
    ).all()
    latest: dict[str, PolicyEvaluation] = {}
    for row in rows:
        latest.setdefault(row.policy_name, row)
    not_passing = [r for r in latest.values() if r.result != PolicyEvaluationResult.PASS]
    not_passing.sort(key=lambda r: r.result != PolicyEvaluationResult.FAIL)
    return [
        {
            "policy_name": row.policy_name,
            "result": row.result.value,
            "observed_value": row.observed_value,
            "threshold": row.threshold,
            "detail": row.detail,
        }
        for row in not_passing
    ]


def build_payload(
    db: Session,
    deployment: Deployment,
    previous: DeploymentStatus,
    target: DeploymentStatus,
    message: str,
    operator: str | None,
) -> dict[str, Any]:
    decided_by = _decided_by(target, message)
    checks = _latest_checks(db, deployment.id)
    headline = _HEADLINE[target].format(stable=deployment.stable_version)
    text = f"{deployment.model_name}: canary {deployment.canary_version} {headline} ({decided_by})"
    failing = [c for c in checks if c["result"] == PolicyEvaluationResult.FAIL.value]
    if target == DeploymentStatus.ROLLED_BACK and failing:
        text += " - failed: " + ", ".join(
            c["policy_name"] + (f" ({c['detail']})" if c["detail"] else "") for c in failing
        )
    url = f"{control_plane_settings.dashboard_url.rstrip('/')}/deployments/{deployment.id}"
    return {
        "event": NOTIFY_ON[target],
        # Chat webhooks (Slack, Mattermost, Teams' incoming webhooks) render
        # this field; everything else is for programs.
        "text": f"{text}\n{url}",
        "occurred_at": datetime.now(UTC).isoformat(),
        "deployment": {
            "id": deployment.id,
            "model_name": deployment.model_name,
            "stable_version": deployment.stable_version,
            "canary_version": deployment.canary_version,
            "status": target.value,
            "url": url,
        },
        "transition": {
            "from": previous.value,
            "to": target.value,
            "reason": message,
            "decided_by": decided_by,
            "operator": operator,
        },
        "checks": checks,
    }


def enqueue(
    db: Session,
    deployment: Deployment,
    previous: DeploymentStatus,
    target: DeploymentStatus,
    message: str,
    operator: str | None,
) -> None:
    """Adds one pending delivery per configured URL to the session - the
    caller's commit sends them on their way. No-op for statuses nobody needs
    to hear about, and when no URL is configured."""
    urls = control_plane_settings.webhook_urls
    if target not in NOTIFY_ON or not urls:
        return
    payload = build_payload(db, deployment, previous, target, message, operator)
    for url in urls:
        db.add(
            WebhookDelivery(
                deployment_id=deployment.id, event=payload["event"], url=url, payload=payload
            )
        )
