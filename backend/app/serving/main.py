import logging
import time
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.serving.config import ServingSettings, serving_settings
from app.serving.fault_injection import apply_fault_injection
from app.serving.model_loader import LoadedModel, load_model
from app.serving.schemas import (
    FaultInjectionIn,
    FaultInjectionOut,
    PredictionResponse,
    build_request_model,
)

logger = logging.getLogger("model_serving")


def create_app(settings: ServingSettings) -> FastAPI:
    """Build a serving app bound to a single (model_name, model_version) pair.

    A factory (rather than a bare module-level app) so tests can spin up
    independently-configured instances - e.g. one per model version, or one with
    fault injection enabled - without relying on process-wide environment state.
    """
    app = FastAPI(title=f"ModelOps Serving - {settings.model_name}/{settings.model_version}")

    loaded_model: LoadedModel | None
    load_error: str | None
    try:
        loaded_model = load_model(
            settings.artifacts_dir, settings.model_name, settings.model_version
        )
        load_error = None
    except Exception as exc:  # noqa: BLE001 - surfaced via /ready instead of crashing startup
        loaded_model = None
        load_error = str(exc)

    request_model = build_request_model(loaded_model.features if loaded_model else [])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "model_name": settings.model_name,
            "model_version": settings.model_version,
        }

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        if loaded_model is None:
            raise HTTPException(status_code=503, detail=f"Model not loaded: {load_error}")
        return {
            "status": "ready",
            "model_name": settings.model_name,
            "model_version": settings.model_version,
            "features": loaded_model.features,
        }

    @app.get("/fault-injection", response_model=FaultInjectionOut)
    def get_fault_injection() -> FaultInjectionOut:
        return FaultInjectionOut(
            latency_ms=settings.injected_latency_ms,
            error_rate=settings.injected_error_rate,
            environment=settings.environment,
        )

    @app.put("/fault-injection", response_model=FaultInjectionOut)
    def put_fault_injection(payload: FaultInjectionIn) -> FaultInjectionOut:
        """Lets a benchmark turn a fault on/off for this container without a
        restart (see backend/scripts/benchmarks/run_benchmark.py). Forbidden in
        production - `settings.is_production` is enforced here (before the update
        is even attempted) AND inside set_fault_injection itself (defense in
        depth, same pattern as apply_fault_injection's own is_production check).
        """
        if settings.is_production:
            raise HTTPException(
                status_code=403, detail="Fault injection is disabled in production"
            )
        settings.set_fault_injection(payload.latency_ms, payload.error_rate)
        return FaultInjectionOut(
            latency_ms=settings.injected_latency_ms,
            error_rate=settings.injected_error_rate,
            environment=settings.environment,
        )

    @app.post("/predict", response_model=PredictionResponse)
    def predict(payload: dict[str, Any]) -> PredictionResponse:
        if loaded_model is None:
            raise HTTPException(status_code=503, detail=f"Model not loaded: {load_error}")

        try:
            validated = request_model(**payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

        apply_fault_injection(settings)

        start = time.perf_counter()
        row = pd.DataFrame([validated.model_dump()])[loaded_model.features]
        prediction = int(loaded_model.pipeline.predict(row)[0])
        probability = float(loaded_model.pipeline.predict_proba(row)[0][1])
        latency_ms = (time.perf_counter() - start) * 1000

        logger.info(
            "prediction served model_name=%s model_version=%s latency_ms=%.3f prediction=%d",
            settings.model_name,
            settings.model_version,
            latency_ms,
            prediction,
        )

        return PredictionResponse(
            prediction=prediction,
            fraud_probability=probability,
            model_name=settings.model_name,
            model_version=settings.model_version,
            latency_ms=latency_ms,
        )

    return app


app = create_app(serving_settings)
