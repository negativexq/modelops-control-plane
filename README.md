# ModelOps Control Plane

A lightweight ModelOps platform that rolls out new ML model versions via controlled
canary deployments, with policy-based promotion/rollback.

## Stack

- **Backend:** FastAPI + SQLAlchemy + SQLite + Alembic + pytest
- **Frontend:** Next.js + TypeScript + Tailwind + Recharts
- **Model serving:** scikit-learn + joblib (planned)
- **Runtime:** Docker + Docker Compose

Designed to run comfortably on a 16 GB RAM machine; heavy components like Kubernetes,
MLflow, and Prometheus are not part of it yet.

## Structure

```
backend/    FastAPI service
frontend/   Next.js dashboard
```

Separate folders for model serving and router services will be added in future sprints.

## Development

```bash
make dev     # bring up backend + frontend via docker compose
make test    # run backend tests
make lint    # backend (ruff, mypy) and frontend (eslint, tsc) lint/type-check
make down    # stop the services
```

- Backend: http://localhost:8000/health
- Frontend: http://localhost:3000/api/health

### Fraud model registry (Sprint 1)

```bash
make generate-data     # synthetic fraud dataset -> backend/data/*.csv
make train-models      # train v1 / v2-good / v2-quality-bad -> backend/artifacts/fraud-model/
make evaluate-models   # check trained artifacts against promotion-gate thresholds
make prepare-models    # generate-data + train-models + evaluate-models
```

Once artifacts exist, the local model registry is browsable via:

- `GET /api/models`
- `GET /api/models/{model_name}/versions`
- `GET /api/models/{model_name}/versions/{version}`
- `GET /api/models/{model_name}/versions/{version}/evaluation`

### Model serving (Sprint 2)

Each model version runs as its own process/container of the same image
(`app.serving.main:app`), selected via `MODEL_NAME` / `MODEL_VERSION` env vars. After
`make prepare-models`, bring up v1 and v2-good side by side:

```bash
docker compose up model-serving-v1 model-serving-v2-good
```

- `GET /health` — liveness + which model_name/model_version this process serves
- `GET /ready` — 503 until the artifact is loaded; otherwise returns the version's feature list
- `POST /predict` — body must match that version's own feature schema (see `/ready`);
  response includes `prediction`, `fraud_probability`, and `latency_ms`

Fault injection for chaos/promotion-gate testing (off by default, and forced off whenever
`ENVIRONMENT=production`, regardless of what these are set to):

- `INJECTED_LATENCY_MS` — artificial delay before each prediction
- `INJECTED_ERROR_RATE` — probability (0-1) of returning a 500 instead of predicting

### Weighted traffic router (Sprint 3)

`app.router.main:app` splits traffic across a list of `{version, weight}` targets.
It reads *where* each version is running from its own static config
(`ROUTER_VERSION_HOSTS`) — never from the model registry, and never from the control
plane, which only ever sends "version -> weight".

```bash
docker compose up model-serving-v1 model-serving-v2-good router
curl -X POST localhost:8080/router/predict -d '{...}' -H 'content-type: application/json'
```

- `POST /router/predict` — weighted-random pick across `targets`, forwards the
  request body **unmodified** (no schema validation in the router — each version owns
  its own `/predict` schema; downstream returns 422 on a bad shape, forwarded as-is).
  Response gets a `routed_to: "<version>"` field added.
- `GET /router/config` / `PUT /router/config` — read/replace `model_name` and
  `targets: [{version, weight}, ...]` at runtime, no restart needed. `PUT` rejects any
  target whose version has no entry in `ROUTER_VERSION_HOSTS` (400).
- `GET /router/health` — router's own liveness plus each configured target's live
  `/ready` status.

Safety behavior:
- If the target picked by the weighted draw is not `/ready`, or is unreachable, the
  router returns **503 and logs a warning** — it does **not** silently fall back to
  another target. Silent failover would let a broken canary hide behind another
  target's success rate, defeating the point of a canary rollout.
- Upstream calls use a **5s timeout and no retries** (`ROUTER_UPSTREAM_TIMEOUT_SECONDS`,
  via `RouterSettings.upstream_timeout_seconds`). This is deliberate: retries would mask
  the transient failures a promotion decision needs to see, and could double-apply
  side effects on the model service. A caller of the router that wants retries should
  add them explicitly on its own side.

### Control plane & deployment lifecycle (Sprint 4)

The `backend` service (`app.main:app`, port 8000) is the control plane and the single
source of truth for traffic allocation — the router's own config is just a cache,
refreshed by the control plane on every change and best-effort re-synced by the router
once at its own startup.

```bash
make migrate    # apply Alembic migrations (creates deployments/traffic_allocations/deployment_events)
docker compose up backend router model-serving-v1 model-serving-v2-good
```

- `POST /api/deployments` — start a new canary deployment. Send an `Idempotency-Key`
  header to make retries safe: a repeated request with the same key returns the
  existing deployment (200) instead of creating a duplicate (first call: 201).
- `GET /api/deployments` / `GET /api/deployments/{id}` — list / inspect a deployment,
  including its full `events` audit log and current `traffic_allocation`.
- `POST /api/deployments/{id}/promote` — shifts the router to 100% canary, deployment
  status becomes `PROMOTED`.
- `POST /api/deployments/{id}/rollback` — shifts the router back to 100% stable,
  status becomes `ROLLED_BACK`.
- `GET /api/router-config/{model_name}` — the router's startup-sync source: the
  currently-**active** traffic allocation for that model (status `CANARY_RUNNING` or
  `EVALUATING` — see `service.get_active_deployment`; a `FAILED`/`ROLLED_BACK`/
  `PROMOTED` deployment is never returned here even if it's the most recent row).

State machine (invalid transitions return 409):

```
PENDING -> DEPLOYING -> CANARY_RUNNING -> EVALUATING -> PROMOTING -> PROMOTED
                                                      -> ROLLING_BACK -> ROLLED_BACK
                                                      -> INCONCLUSIVE -> (promote/rollback)
any in-flight state -> FAILED
```

`promote`/`rollback` auto-advance `CANARY_RUNNING -> EVALUATING` first if needed, then
require `EVALUATING` or `INCONCLUSIVE`. Every transition is recorded as a
`DeploymentEvent` (visible in the deployment's `events` list), forming the audit trail.

The control plane never resolves or sends a version's host/port to the router — only
`{version, weight}` pairs (`app/control_plane/router_gateway.py`). If the router push
fails, the deployment is marked `FAILED` (with the reason logged as an event) rather
than left in an ambiguous in-flight state.

`RouterGateway` reuses a single `httpx.AsyncClient` created once in `app.main`'s
lifespan (`app.state.http_client`) rather than opening/closing one per request.

### Metrics collection (Sprint 5)

The router publishes one metric per forward; the control plane stores it and computes
p50/p95/p99 latency and error rate per version, per deployment, over a sliding window.

- `POST /api/deployments/{id}/metrics` — what the router calls after every
  `/router/predict` forward. Deliberately minimal (one PK existence check, one insert,
  no joins, no state-machine/event-log involvement) since this is a hot path.
- `GET /api/deployments/{id}/metrics?window_seconds=300` — `stable`/`canary` computed
  separately: `sample_count`, `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms`,
  `error_rate` (share of `status_code >= 400`), plus `precision`/`recall`/
  `false_positive_rate`. The latter three are **only computed for samples where
  `actual_label` has been backfilled** — there's no label source yet in this sprint, so
  in practice they're `None` until something starts populating it.
- `GET /api/deployments/{id}/comparison?window_seconds=300` — same two summaries plus
  a `deltas` object (`canary - stable` for `p95_latency_ms`, `error_rate`, `recall`;
  `None` if either side has no samples in the window).

How the router publishes without slowing `/predict` down: after forwarding and
building the response, it schedules `asyncio.create_task(send_metric(...))` and
returns immediately — it does **not** use FastAPI's `BackgroundTasks`, because
Starlette awaits those as part of the same ASGI response cycle (so a slow metrics
call would still show up as added latency to anything measuring end-to-end). A bare
`create_task` is genuinely decoupled: the response is already on its way to the
client before that task runs. Task references are kept in a small set with a
done-callback (`app/router/main.py`) purely so asyncio doesn't warn about a
garbage-collected pending task — nothing awaits them as part of request handling. If
the metric POST fails or the control plane is unreachable, it's caught and logged
inside `send_metric` (`app/router/metrics.py`); `/predict` already returned its result
by then regardless.

p50/p95/p99 are computed in Python (`app/control_plane/metrics_service.py`), not SQL —
SQLite has no `percentile_cont` or window-function equivalent. The window query pulls
matching rows (`deployment_id` + `model_version` + `created_at >= now - window`) and
computes linear-interpolation percentiles (same convention as `numpy.percentile`'s
default) over the in-memory list.

### Dashboard (Sprint 6)

The frontend (`frontend/src/app`) is a set of Client Components that call the control
plane's REST API directly from the browser via `NEXT_PUBLIC_API_URL` (defaults to
`http://localhost:8000`) — no Next.js server-side data fetching, since every page
needs an explicit, non-optimistic refetch after mutations (see below). The backend
allows this with `CORSMiddleware` (`MODELOPS_CORS_ALLOW_ORIGINS`, defaults to
`http://localhost:3000`).

```bash
make prepare-models && make migrate
docker compose up backend router model-serving-v1 model-serving-v2-good frontend
# or, for local iteration without docker:
#   cd backend && .venv/bin/uvicorn app.main:app --port 8000 --reload
#   cd frontend && npm run dev
```

- **Overview** (`/`) — active model, stable/canary version, traffic split, latest
  deployment's status.
- **Models** (`/models`) — versions per model with their offline evaluation metrics,
  plus the "start a new canary deployment" form (`POST /api/deployments`).
- **Deployments** (`/deployments`) — history list with inline Promote/Rollback.
- **Deployment detail** (`/deployments/[id]`) — full event log, traffic split, and the
  Canary Analysis charts (latency p50/p95/p99 and error rate, stable vs canary, via
  `GET .../comparison`).

Shared pieces:

- `lib/api.ts` — one typed function per backend endpoint; throws `ApiError` uniformly.
- `lib/useAsync.ts` — fetch-on-mount-and-deps-change with an explicit `refetch()`.
  Deliberately no cache/optimistic layer.
- `lib/useMutation.ts` — wraps promote/rollback/create-deployment calls; exposes
  `submitting` + a `{kind, message}` `result`, used to show a banner **after the real
  API response comes back**, then the caller's `refetch()` re-reads actual state.
  There's no local "assume it worked" update anywhere.
- `components/AsyncBoundary.tsx` — the one loading/error pattern every page uses.
- `lib/format.ts` — `formatNumber`/`formatMs`/`formatPercent` render `null` as
  **"N/A"**, never "0" — load-bearing for the comparison view, since
  precision/recall/false-positive-rate are `null` (not zero) until something backfills
  `actual_label`.
