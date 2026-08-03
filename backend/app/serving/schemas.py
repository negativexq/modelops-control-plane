from pydantic import BaseModel, Field, create_model

# Business-column dtypes for the fraud dataset (see backend/scripts/generate_dataset.py).
# Serving does not import that script (it isn't shipped in the runtime image), so the
# handful of non-numeric-index columns are duplicated here as a small, stable contract.
_BUSINESS_FEATURE_TYPES: dict[str, type] = {
    "amount": float,
    "merchant_category": str,
    "transaction_hour": int,
    "device_risk_score": float,
    "customer_age_days": int,
    "previous_declines": int,
    "country_mismatch": int,
    "is_new_customer": int,
}


def _feature_type(name: str) -> type:
    if name in _BUSINESS_FEATURE_TYPES:
        return _BUSINESS_FEATURE_TYPES[name]
    # feature_0..feature_N from make_classification are always numeric.
    return float


def build_request_model(features: list[str]) -> type[BaseModel]:
    """Build a pydantic request model from a version's own metadata feature list.

    Each model version can declare a different feature subset (e.g. v2-quality-bad
    trains on a deliberately reduced set) — the request schema must follow suit rather
    than assuming the full column set.
    """
    fields: dict[str, tuple[type, object]] = {
        name: (_feature_type(name), ...) for name in features
    }
    return create_model("PredictionRequest", **fields)  # type: ignore[call-overload,no-any-return]


class PredictionResponse(BaseModel):
    prediction: int
    fraud_probability: float
    model_name: str
    model_version: str
    latency_ms: float


class FaultInjectionIn(BaseModel):
    latency_ms: int = Field(ge=0)
    error_rate: float = Field(ge=0, le=1)


class FaultInjectionOut(BaseModel):
    latency_ms: int
    error_rate: float
    environment: str
