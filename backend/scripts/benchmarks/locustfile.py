"""Locust load definition for the ModelOps router. Run via load_runner.py (which
shells out to `locust --headless -f locustfile.py ...`) - not meant to be run
directly, though `locust -f locustfile.py --host http://localhost:8080` works fine
for ad hoc use.

Sends one consistent full-feature-superset payload to POST /router/predict
regardless of which scenario is active: every trained version's dynamic pydantic
request model (see app/serving/schemas.py) ignores fields it doesn't need, so the
same payload works whether the router forwards to v1, v2-good, or v2-quality-bad.
"""

import os
import random

from locust import HttpUser, constant_pacing, task

MERCHANT_CATEGORIES = [
    "grocery",
    "electronics",
    "travel",
    "gaming",
    "fashion",
    "restaurants",
    "utilities",
    "subscriptions",
    "jewelry",
    "crypto",
]

TARGET_RPS = float(os.environ.get("BENCHMARK_TARGET_RPS", "100"))
USER_COUNT = int(os.environ.get("LOCUST_USER_COUNT", "20"))
# Each user waits this long between requests so that USER_COUNT users together
# average out to TARGET_RPS total (constant_pacing keeps each user's own request
# rate steady even if individual requests are slow).
_PACING_SECONDS = max(USER_COUNT / TARGET_RPS, 0.001)


def _sample_payload() -> dict[str, object]:
    payload: dict[str, object] = {f"feature_{i}": random.gauss(0, 1) for i in range(20)}
    payload.update(
        {
            "amount": round(random.uniform(5, 500), 2),
            "merchant_category": random.choice(MERCHANT_CATEGORIES),
            "transaction_hour": random.randint(0, 23),
            "device_risk_score": round(random.uniform(0, 1), 4),
            "customer_age_days": random.randint(1, 3650),
            "previous_declines": random.randint(0, 3),
            "country_mismatch": random.choice([0, 1]),
            "is_new_customer": random.choice([0, 1]),
        }
    )
    return payload


class RouterUser(HttpUser):
    wait_time = constant_pacing(_PACING_SECONDS)

    @task
    def predict(self) -> None:
        self.client.post("/router/predict", json=_sample_payload(), name="/router/predict")
