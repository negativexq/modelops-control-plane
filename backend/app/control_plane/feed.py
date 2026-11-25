"""Change feed behind GET /api/stream (Server-Sent Events).

Every write that matters to an operator already leaves a row behind: state changes,
traffic advances, pauses and router reachability flips log a DeploymentEvent, and
every policy check logs a PolicyEvaluation. So the feed doesn't need a message bus
or in-process pub/sub (which would miss writes from other processes anyway); it
tails those two tables.

Rows are read with a lookback window rather than a strict "newer than the last one
seen" cursor, because created_at is stamped in Python before commit: a transaction
that stamps earlier but commits later would otherwise slip behind the cursor and
never be sent. Ids already sent inside the window are remembered so nothing is sent
twice.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.models import DeploymentEvent, PolicyEvaluation

LOOKBACK = timedelta(seconds=10)


def _aware(value: datetime) -> datetime:
    # SQLite hands timezone=True columns back naive; everything is stored as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass
class FeedCursor:
    since: datetime = field(default_factory=lambda: datetime.now(UTC))
    sent: dict[str, datetime] = field(default_factory=dict)


def poll_changes(db: Session, cursor: FeedCursor) -> list[dict[str, Any]]:
    """New events and policy results since `cursor`, oldest first. Mutates `cursor`."""
    floor = cursor.since - LOOKBACK
    items: list[tuple[datetime, str, dict[str, Any]]] = []

    events = db.execute(
        select(DeploymentEvent).where(DeploymentEvent.created_at >= floor)
    ).scalars()
    for event in events:
        at = _aware(event.created_at)
        items.append(
            (
                at,
                event.id,
                {
                    "kind": "event",
                    "deployment_id": event.deployment_id,
                    "event_type": event.event_type,
                    "message": event.message,
                    "actor": event.actor,
                    "at": at.isoformat(),
                },
            )
        )

    evaluations = db.execute(
        select(PolicyEvaluation).where(PolicyEvaluation.evaluated_at >= floor)
    ).scalars()
    for evaluation in evaluations:
        at = _aware(evaluation.evaluated_at)
        items.append(
            (
                at,
                evaluation.id,
                {
                    "kind": "policy",
                    "deployment_id": evaluation.deployment_id,
                    "policy_name": evaluation.policy_name,
                    "result": evaluation.result.value,
                    "at": at.isoformat(),
                },
            )
        )

    items.sort(key=lambda item: item[0])
    fresh = [(at, row_id, payload) for at, row_id, payload in items if row_id not in cursor.sent]
    for at, row_id, _ in fresh:
        cursor.sent[row_id] = at
        cursor.since = max(cursor.since, at)

    # Forget ids that have aged out of the window; they can't be read again.
    new_floor = cursor.since - LOOKBACK
    cursor.sent = {row_id: at for row_id, at in cursor.sent.items() if at >= new_floor}
    return [payload for _, _, payload in fresh]


def prime_cursor(db: Session) -> FeedCursor:
    """A cursor positioned at "now": rows already in the window count as sent, so
    a fresh connection only hears about changes made after it opened."""
    cursor = FeedCursor()
    poll_changes(db, cursor)
    return cursor
