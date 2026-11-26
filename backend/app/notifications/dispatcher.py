"""Sends queued decision notifications (see outbox.py) and retries the ones a
receiver didn't take.

Each delivery is a POST of the payload as JSON, with:

- X-ModelOps-Event: the event name, e.g. deployment.rolled_back
- X-ModelOps-Delivery: the delivery id - the same on every retry, so a
  receiver can drop duplicates (delivery is at-least-once: a receiver that
  took the request but timed out answering gets it again)
- X-ModelOps-Signature: sha256=<hex HMAC-SHA256 of the body with the shared
  secret>, when CONTROL_PLANE_WEBHOOK_SECRET is set

A 2xx answer delivers it. Anything else - an error status, a timeout, a
refused connection - is retried with exponential backoff (2, 4, 8 ... capped
at 60 seconds) up to webhook_max_attempts, then marked FAILED and left in the
table for GET /api/notifications/deliveries to show.

The backend runs this in a daemon thread (start()) when any webhook URL is
configured. With more than one backend replica each would send the same rows;
claiming a row (UPDATE ... WHERE status = PENDING) before sending is the
change that would need, and isn't worth it for a single-replica SQLite stack.
"""

import hashlib
import hmac
import json
import logging
import threading
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.control_plane import telemetry
from app.control_plane.config import control_plane_settings
from app.control_plane.models import WebhookDelivery, WebhookDeliveryStatus

logger = logging.getLogger("control_plane.notifications")

MAX_BACKOFF_SECONDS = 60
BATCH_SIZE = 50
POLL_INTERVAL_SECONDS = 1.0


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(2**attempts, MAX_BACKOFF_SECONDS))


def _as_utc(value: datetime) -> datetime:
    # SQLite hands datetimes back naive; they were written in UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _send(client: httpx.Client, delivery: WebhookDelivery) -> tuple[int | None, str | None]:
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "modelops-control-plane",
        "X-ModelOps-Event": delivery.event,
        "X-ModelOps-Delivery": delivery.id,
    }
    if control_plane_settings.webhook_secret:
        headers["X-ModelOps-Signature"] = sign(body, control_plane_settings.webhook_secret)
    try:
        response = client.post(
            delivery.url,
            content=body,
            headers=headers,
            timeout=control_plane_settings.webhook_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:500]
    if response.is_success:
        return response.status_code, None
    return response.status_code, f"HTTP {response.status_code}: {response.text[:200]}"


def deliver_due(db: Session, client: httpx.Client, now: datetime | None = None) -> int:
    """Attempts every pending delivery whose time has come; returns how many
    were attempted. Each attempt is committed on its own, so a crash mid-batch
    loses at most the one in flight (which is then sent again)."""
    now = now or datetime.now(UTC)
    due = db.scalars(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == WebhookDeliveryStatus.PENDING,
            or_(
                WebhookDelivery.next_attempt_at.is_(None),
                WebhookDelivery.next_attempt_at <= now,
            ),
        )
        .order_by(WebhookDelivery.created_at)
        .limit(BATCH_SIZE)
    ).all()
    for delivery in due:
        status_code, error = _send(client, delivery)
        delivery.attempts += 1
        delivery.last_status_code = status_code
        delivery.last_error = error
        if error is None:
            delivery.status = WebhookDeliveryStatus.DELIVERED
            delivery.delivered_at = datetime.now(UTC)
            result = "delivered"
        elif delivery.attempts >= control_plane_settings.webhook_max_attempts:
            delivery.status = WebhookDeliveryStatus.FAILED
            result = "failed"
            logger.warning(
                "giving up on %s to %s after %d attempts: %s",
                delivery.event,
                delivery.url,
                delivery.attempts,
                error,
            )
        else:
            delivery.next_attempt_at = _as_utc(now) + _backoff(delivery.attempts)
            result = "retrying"
        db.commit()
        telemetry.WEBHOOK_DELIVERIES.labels(event=delivery.event, result=result).inc()
    return len(due)


class Dispatcher:
    """The background loop around deliver_due."""

    def __init__(self, sessions: Callable[[], Generator[Session]]) -> None:
        self._sessions = sessions
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="webhook-dispatcher", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        with httpx.Client() as client:
            while not self._stop.is_set():
                sessions = self._sessions()
                try:
                    deliver_due(next(sessions), client)
                except Exception:
                    logger.exception("webhook dispatch failed")
                finally:
                    sessions.close()
                self._stop.wait(POLL_INTERVAL_SECONDS)


def start(sessions: Callable[[], Generator[Session]]) -> Dispatcher | None:
    """Starts the dispatcher if there's anywhere to deliver to."""
    if not control_plane_settings.webhook_urls:
        return None
    dispatcher = Dispatcher(sessions)
    dispatcher.start()
    logger.info("delivering decision notifications to %s", control_plane_settings.webhook_urls)
    return dispatcher
