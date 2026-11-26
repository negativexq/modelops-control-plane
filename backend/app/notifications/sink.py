"""A webhook receiver for the local stack and CI: keeps what the control plane
sends in memory and shows it at GET /received, so you can watch decision
notifications arrive without a Slack workspace.

    uvicorn app.notifications.sink:app --port 8000

With WEBHOOK_SECRET set it checks each X-ModelOps-Signature and records
whether it matched (it still answers 2xx either way, so a wrong secret shows up
in /received rather than as endless retries). WEBHOOK_SINK_FAIL_FIRST=N answers
the first N deliveries with 503, to watch the retries work.
"""

import hmac
import json
import os
from collections import deque
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response

from app.notifications.dispatcher import sign

app = FastAPI(title="ModelOps webhook sink")

_received: deque[dict[str, Any]] = deque(maxlen=500)
_fail_first = int(os.environ.get("WEBHOOK_SINK_FAIL_FIRST", "0"))
_seen = 0


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/hooks")
async def receive(request: Request) -> Response:
    global _seen
    _seen += 1
    if _seen <= _fail_first:
        return Response(status_code=503)
    body = await request.body()
    secret = os.environ.get("WEBHOOK_SECRET")
    signature = request.headers.get("x-modelops-signature")
    verified = (
        None
        if not secret
        else signature is not None and hmac.compare_digest(signature, sign(body, secret))
    )
    _received.append(
        {
            "received_at": datetime.now(UTC).isoformat(),
            "event": request.headers.get("x-modelops-event"),
            "delivery_id": request.headers.get("x-modelops-delivery"),
            "signature_verified": verified,
            "payload": json.loads(body),
        }
    )
    return Response(status_code=204)


@app.get("/received")
def received(event: str | None = None) -> list[dict[str, Any]]:
    """Newest first."""
    return [r for r in reversed(_received) if event is None or r["event"] == event]
