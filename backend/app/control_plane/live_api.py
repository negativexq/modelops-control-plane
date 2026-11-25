"""GET /api/stream (Server-Sent Events) and GET /api/fleet/summary."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.control_plane.feed import FeedCursor, poll_changes, prime_cursor
from app.control_plane.fleet import FleetSummary, build_summary
from app.db import SessionLocal, get_db

router = APIRouter(prefix="/api", tags=["live"])

DbDep = Annotated[Session, Depends(get_db)]

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
# Streams are closed and re-opened (EventSource reconnects on its own after
# `retry`) so a dropped client or a `--reload` never pins a worker forever.
MAX_STREAM_SECONDS = 300.0
RETRY_MS = 2000


def get_session_factory() -> Callable[[], Session]:
    """A stream outlives any single request-scoped session, so it opens a short
    session per poll instead. Overridable in tests."""
    return SessionLocal


T = TypeVar("T")

SessionFactoryDep = Annotated[Callable[[], Session], Depends(get_session_factory)]


def _with_session(factory: Callable[[], Session], fn: Callable[[Session], T]) -> T:
    db = factory()
    try:
        return fn(db)
    finally:
        db.close()


def sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def change_stream(
    request: Request,
    factory: Callable[[], Session],
    *,
    poll_seconds: float = POLL_SECONDS,
    max_seconds: float = MAX_STREAM_SECONDS,
) -> AsyncIterator[str]:
    cursor: FeedCursor = await run_in_threadpool(_with_session, factory, prime_cursor)
    yield f"retry: {RETRY_MS}\n\n"
    yield sse("ready", {"at": cursor.since.isoformat()})

    started = last_sent = time.monotonic()
    while time.monotonic() - started < max_seconds:
        if await request.is_disconnected():
            return
        changes = await run_in_threadpool(
            _with_session, factory, lambda db: poll_changes(db, cursor)
        )
        for change in changes:
            yield sse("change", change)
        now = time.monotonic()
        if changes:
            last_sent = now
        elif now - last_sent >= HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_sent = now
        await asyncio.sleep(poll_seconds)


@router.get("/stream")
async def stream(request: Request, factory: SessionFactoryDep) -> StreamingResponse:
    """Server-Sent Events: one `change` event per new DeploymentEvent or
    PolicyEvaluation row (see app/control_plane/feed.py). The dashboard uses it to
    refetch exactly what changed instead of polling every few seconds."""
    return StreamingResponse(
        change_stream(request, factory),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/fleet/summary", response_model=FleetSummary)
def fleet_summary(
    db: DbDep,
    include_benchmarks: bool = True,
    days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> FleetSummary:
    return build_summary(db, include_benchmarks=include_benchmarks, days=days)
