import random
import time

from fastapi import HTTPException

from app.serving.config import ServingSettings


def apply_fault_injection(settings: ServingSettings) -> None:
    """Apply configured artificial latency / error-rate, if any.

    Defense in depth: ServingSettings already forces both knobs to zero when
    environment=="production" (see config.py), and this function re-checks
    `is_production` itself so fault injection can never fire in production even
    if a future caller constructs settings differently.
    """
    if settings.is_production:
        return

    if settings.injected_latency_ms > 0:
        time.sleep(settings.injected_latency_ms / 1000)

    if settings.injected_error_rate > 0 and random.random() < settings.injected_error_rate:
        raise HTTPException(status_code=500, detail="Injected fault (INJECTED_ERROR_RATE)")
