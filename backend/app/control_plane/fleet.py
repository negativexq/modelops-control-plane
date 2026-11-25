"""Fleet-wide rollout summary behind GET /api/fleet/summary.

The dashboard used to compute these numbers itself from the full deployment list
(every row, every event). That works for a demo, but the cost grows with history
and the logic for "who made this decision" lived in TypeScript only. Here it's
computed once, next to the code that writes those events.
"""

import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.control_plane.models import Deployment, DeploymentStatus

Outcome = Literal["PROMOTED", "ROLLED_BACK", "INCONCLUSIVE", "FAILED"]
Actor = Literal["automatic", "manual", "unknown"]

OUTCOMES: tuple[Outcome, ...] = ("PROMOTED", "ROLLED_BACK", "INCONCLUSIVE", "FAILED")

# Statuses where the worker or router can still change things on their own.
# INCONCLUSIVE is not one of them: it's frozen until a human acts.
LIVE_STATUSES = frozenset(
    {
        DeploymentStatus.PENDING,
        DeploymentStatus.DEPLOYING,
        DeploymentStatus.CANARY_RUNNING,
        DeploymentStatus.EVALUATING,
        DeploymentStatus.PROMOTING,
        DeploymentStatus.ROLLING_BACK,
    }
)


class OutcomeCount(BaseModel):
    total: int = 0
    automatic: int = 0
    manual: int = 0
    unknown: int = 0


class DailyOutcomes(BaseModel):
    date: date
    PROMOTED: int = 0
    ROLLED_BACK: int = 0
    INCONCLUSIVE: int = 0
    FAILED: int = 0


class FleetSummary(BaseModel):
    generated_at: datetime
    total: int
    live: int
    by_status: dict[str, int]
    outcomes: dict[str, OutcomeCount]
    # Share of PROMOTED/ROLLED_BACK decisions the policy engine made on its own.
    automation_rate: float | None
    # Median seconds from start to PROMOTED/ROLLED_BACK.
    median_decision_seconds: float | None
    daily: list[DailyOutcomes]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def decision_actor(deployment: Deployment) -> Actor:
    """Who made the final decision. promote/rollback tag their final status_changed
    message with "(automatic)" or "(manual)"; INCONCLUSIVE is only ever reached by
    the worker."""
    if deployment.status == DeploymentStatus.INCONCLUSIVE:
        return "automatic"
    marker = f"-> {deployment.status.value}"
    for event in sorted(deployment.events, key=lambda e: _aware(e.created_at), reverse=True):
        if event.event_type == "status_changed" and marker in event.message:
            if "(automatic)" in event.message:
                return "automatic"
            if "(manual)" in event.message:
                return "manual"
            break
    return "unknown"


def _decided_at(deployment: Deployment) -> datetime | None:
    if deployment.completed_at is not None:
        return _aware(deployment.completed_at)
    if deployment.events:
        return max(_aware(e.created_at) for e in deployment.events)
    return None


def build_summary(
    db: Session, *, include_benchmarks: bool = True, days: int = 14, now: datetime | None = None
) -> FleetSummary:
    now = now or datetime.now(UTC)
    stmt = select(Deployment).options(selectinload(Deployment.events))
    deployments = list(db.execute(stmt).scalars())
    if not include_benchmarks:
        deployments = [d for d in deployments if not d.model_name.startswith("benchmark-")]

    by_status: dict[str, int] = {}
    outcomes: dict[str, OutcomeCount] = {outcome: OutcomeCount() for outcome in OUTCOMES}
    today = now.date()
    daily = [DailyOutcomes(date=today - timedelta(days=days - 1 - i)) for i in range(days)]
    daily_index = {bucket.date: bucket for bucket in daily}
    decision_seconds: list[float] = []

    for deployment in deployments:
        status = deployment.status.value
        by_status[status] = by_status.get(status, 0) + 1
        if status not in outcomes:
            continue

        count = outcomes[status]
        count.total += 1
        actor = decision_actor(deployment)
        setattr(count, actor, getattr(count, actor) + 1)

        decided_at = _decided_at(deployment)
        if decided_at is not None and (bucket := daily_index.get(decided_at.date())):
            setattr(bucket, status, getattr(bucket, status) + 1)
        if (
            status in ("PROMOTED", "ROLLED_BACK")
            and decided_at is not None
            and deployment.started_at is not None
        ):
            elapsed = (decided_at - _aware(deployment.started_at)).total_seconds()
            decision_seconds.append(max(0.0, elapsed))

    decided = outcomes["PROMOTED"].total + outcomes["ROLLED_BACK"].total
    automatic = outcomes["PROMOTED"].automatic + outcomes["ROLLED_BACK"].automatic

    return FleetSummary(
        generated_at=now,
        total=len(deployments),
        live=sum(1 for d in deployments if d.status in LIVE_STATUSES),
        by_status=by_status,
        outcomes=outcomes,
        automation_rate=automatic / decided if decided else None,
        median_decision_seconds=statistics.median(decision_seconds) if decision_seconds else None,
        daily=daily,
    )
