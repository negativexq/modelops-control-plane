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

These env vars are only the *startup* default. They can also be changed at runtime,
without a restart, via `GET`/`PUT /fault-injection` (`{latency_ms, error_rate}`) -
`PUT` is `403` whenever `ENVIRONMENT=production`, same guard as above. This is what
lets the benchmark suite (see "Benchmark suite" below) toggle fault injection on a
long-lived container over plain HTTP instead of restarting it.

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

### Policy engine (Sprint 7)

`app/policy/` turns a comparison window into a PASS/FAIL/INCONCLUSIVE verdict, per
policy, persisted as an audit trail — it does **not** touch deployment state (that's
Sprint 8's job; promote/rollback are still manual API calls).

```bash
curl -X POST localhost:8000/api/deployments/<id>/evaluate   # uses env-configured defaults
curl localhost:8000/api/deployments/<id>/policy-evaluations  # history, newest first
```

- `PolicyConfig` (`app/policy/config.py`) is a plain Pydantic model, not a DB table
  this sprint: `minimum_requests`, `evaluation_window_seconds`, and nested
  `latency.p95_max_increase_percent` / `reliability.max_error_rate_percent` /
  `quality.minimum_recall`. Defaults come from `PolicySettings` (env vars prefixed
  `POLICY_`, same pattern as `RouterSettings`/`ControlPlaneSettings`); `POST
  .../evaluate` accepts an optional JSON body that overrides some or all of it for
  that one call. `evaluation_window_seconds` is part of the policy, not a query
  param like the comparison endpoint's `window_seconds` - a policy's verdict has to
  be reproducible from the policy alone.
- Four checks run per evaluation, each its own `PolicyEvaluation` row
  (`policy_name`, `metric_name`, `observed_value`, `threshold`, `result`):
  `minimum_requests`, `latency_p95_increase`, `max_error_rate`, `minimum_recall`.
  If `minimum_requests` isn't met by *either* version, that's the **only** row
  written - the other three never run against too little traffic.
- Overall verdict (`app/policy/engine.py::overall_result`): **FAIL beats
  INCONCLUSIVE beats PASS**. One failing check fails the whole evaluation; an
  inconclusive check (most often `minimum_recall`, since no `actual_label` source
  exists yet - see Sprint 5) can never be outvoted into a PASS by the other checks.
  "Couldn't tell" and "looked fine" are never the same bucket.

Nothing here decides anything automatically yet - `POST .../evaluate` only records
what the policies found. A human still calls `/promote` or `/rollback`.

### Automated promotion & rollback (Sprint 8)

`app/worker/` is a separate process that closes the loop: it calls `/evaluate` on
every `CANARY_RUNNING`/`EVALUATING` deployment and acts on the result through the
control plane's own REST API - the same endpoints a human/dashboard would call. It
has **no direct DB access and keeps no in-memory rollout state**; every decision is
re-derived from what `GET /api/deployments/{id}` and `GET .../policy-evaluations`
report *right now*, so restarting it just resumes.

```bash
docker compose up backend router model-serving-v1 model-serving-v2-good worker
```

- **PASS**, canary not yet at 100% → `POST .../advance-traffic` (new this sprint):
  steps the canary through `10% → 25% → 50% → 100%`
  (`control_plane/service.py::TRAFFIC_STAGES`). The next stage is always "the
  smallest stage weight above the canary's *current* weight" read from its live
  `TrafficAllocation` - not a stored index - so it's correct regardless of what
  custom `canary_weight` the deployment started at, and survives a worker restart
  with zero extra bookkeeping.
- **PASS**, canary already at 100% → `POST .../promote?triggered_by=automatic`.
- **FAIL** → `POST .../rollback?triggered_by=automatic`.
- **INCONCLUSIVE** → `POST .../record-inconclusive` (new this sprint): bumps
  `Deployment.inconclusive_retry_count`; once it exceeds the deployment's own
  `policy_config.max_inconclusive_retries`, the deployment is frozen into
  `INCONCLUSIVE` status and the worker stops touching it. A human can still call
  `/promote` or `/rollback` on a frozen deployment - `INCONCLUSIVE` isn't a dead end,
  the state machine already allowed `INCONCLUSIVE -> PROMOTING/ROLLING_BACK` from
  Sprint 4.

`triggered_by=manual|automatic` (default `manual`) on `/promote` and `/rollback`
puts the distinction directly in the `DeploymentEvent` message
(`"manual promote requested"` vs. `"automatic promote requested"`) - the dashboard's
existing buttons don't need to change.

**Policy config is now persisted**, not resolved fresh on every call: `POST
/api/deployments` accepts an optional `policy_config` body that's resolved once
(defaults filled in from `PolicySettings` if omitted) and stored on the deployment
row. `POST .../evaluate` without a body now reads *that* instead of falling back to
env defaults - a deployment's thresholds stay fixed for its lifetime even if the
global defaults change later.

**Dedup**: the worker doesn't just sleep-and-poll `evaluation_window_seconds` - it
checks the deployment's *own* most recent `PolicyEvaluation.evaluated_at`
(`GET .../policy-evaluations`, newest first) and skips re-evaluating until that
policy's window has actually elapsed. This is what makes it restart-safe without a
separate "last checked" store: the record it needs already exists in the same table
being written to.

**Race safety**: every action endpoint (`advance-traffic`, `promote`, `rollback`,
`record-inconclusive`) independently re-checks the deployment is still
`CANARY_RUNNING`/`EVALUATING` server-side and returns **409** otherwise - so if a
human promotes/rolls back a deployment between the worker's `evaluate` call and its
follow-up action, the server rejects the stale action instead of corrupting an
already-final `TrafficAllocation`. The worker treats a 409 as "someone else already
handled this," logs it, and moves on to the next deployment.

### Benchmark suite (Sprint 9)

`backend/scripts/benchmarks/` drives repeatable, end-to-end scenarios against a
running stack (`make dev`, or `docker compose up backend router worker
model-serving-v1 model-serving-v2-good model-serving-v2-quality-bad`) using
**Locust** for load generation - chosen over k6 because it's Python, so the load
definition (`locustfile.py`) and the orchestration (creating deployments, polling
outcomes, computing timings) live in the same language and process family as the
rest of `backend/scripts/`, with no separate JS toolchain. Locust runs as a
subprocess in headless mode (`--csv=...`); the orchestrator parses its stats CSV
rather than embedding Locust's gevent runtime in-process (mixing gevent with the
rest of the codebase's asyncio/httpx would be its own source of bugs).

```bash
make benchmark-baseline          # v1 only, 100 RPS / 5 min - throughput/latency/error-rate
make benchmark-latency-failure   # injected +400ms canary -> expects automatic rollback
make benchmark-error-failure     # injected 50% error-rate canary -> expects automatic rollback
make benchmark-quality-failure   # weak model, no fault -> expects an INCONCLUSIVE freeze
make benchmark-success           # healthy canary -> expects a full automatic promotion
make benchmark-all               # all five, sequentially
```

**Isolation**: each scenario gets its own `model_name` (`benchmark-baseline`,
`benchmark-latency-failure`, ...), so its `Deployment`/`PolicyEvaluation`/
`DeploymentEvent` rows never mix with a real `fraud-model` deployment created from
the dashboard. This does **not** extend to the router: it holds exactly one active
traffic split (`RouterConfigStore` is a single mutable slot, by design since Sprint
3), so **run benchmarks one at a time** - `make benchmark-all` does this
sequentially, and running a benchmark concurrently with a real demo you care about
will overwrite that demo's traffic split for the benchmark's duration.

The two fault-injected scenarios target dedicated containers
(`model-serving-v2-good-latency-fault`, `model-serving-v2-good-error-fault`) that
serve the *same* v2-good artifact as the real `model-serving-v2-good`. These
containers are **always up** as part of the normal stack, with fault injection
**off by default** - there is no container start/stop step. Instead, each serving
app exposes a runtime `GET`/`PUT /fault-injection` endpoint
(`latency_ms`/`error_rate` body, mutates an in-memory settings object; `403` outside
development/benchmark profiles, matching the existing production guard on injected
faults). The benchmark orchestrator (`scripts/benchmarks/run_benchmark.py`) calls
`PUT` on the relevant fault container over plain HTTP before generating load, and
always resets it back to zero in a `finally` block afterward - even if the run
crashes - so a failed or interrupted benchmark can never leave fault injection on
for the next run or for real traffic. No Docker socket or container lifecycle
access is needed anywhere in this path; the backend only ever speaks HTTP to hosts
it already knows about, the same way it already talks to the router
(`ROUTER_VERSION_HOSTS`). The router is told about the fault containers under
distinct version labels, completely decoupled from the serving container's own
self-reported `model_version` - this is the same version-label-vs-artifact
decoupling the router has used since Sprint 3, just exploited here to point a
benchmark's canary at a "broken" instance without touching the real one.

**What "quality regression" actually means today**: there is still no real
`actual_label` source anywhere in the platform (Sprint 5/7), so the recall policy
check is *always* `INCONCLUSIVE`, never `FAIL` - and since `INCONCLUSIVE` beats
`PASS` (Sprint 7's deliberate precedence rule), **no deployment can be automatically
promoted, or even advanced past its first traffic stage, without some source of
ground-truth labels.** `benchmark-quality-failure` demonstrates this honestly: with
no fault injection, latency/error checks pass, but the perpetually-unresolvable
recall check keeps the overall result `INCONCLUSIVE` until
`max_inconclusive_retries` is exceeded and the deployment freezes - the report
explicitly labels the end state as an INCONCLUSIVE freeze, not a "recall FAIL
rollback," because that's what actually happens. To still exercise the automatic
*promotion* path end to end, `benchmark-success` backfills synthetic
`(prediction, actual_label)` pairs via `POST .../metrics` throughout the run - a
simulated stand-in for a delayed label feed that doesn't exist as a real system
component yet. This is called out explicitly in that scenario's own report, not
hidden as if it were real platform behavior.

Each run writes `backend/benchmark-results/{scenario}.json` and `.md`
(gitignored, reproducible) with the full timing/load breakdown, plus **time to
detect** (first matching `PolicyEvaluation` row after rollout start) and **time to
action** (first matching `DeploymentEvent` after rollout start) - both computed from
API-returned timestamps in `scripts/benchmarks/report.py`, not wall-clock guesses.
These naturally land around `poll_interval_seconds + evaluation_window_seconds`
(worker defaults: 15s poll interval; each scenario sets its own, short
`evaluation_window_seconds` to keep runs quick) - that's the real, expected latency
of the automation loop, not a benchmark failure to explain away.

**Triggering benchmarks from the dashboard**: the same five scenarios can be
started and watched from the `/benchmarks` page instead of the CLI. `POST
/api/benchmarks/run` spawns `python -m scripts.benchmarks.run_benchmark` as a
subprocess of the backend (the `scripts/` directory ships inside the backend
image, and `locust` is a core dependency there, not just a dev one) and tracks it
in a `BenchmarkRun` row (`RUNNING`/`COMPLETED`/`FAILED`, started/completed
timestamps, the scenario's JSON report once it lands). Only one run is tracked as
active at a time - `POST /run` returns **409** while another is `RUNNING`, for the
same single-traffic-slot reason the CLI's `make benchmark-all` runs sequentially -
and the dashboard proactively disables every scenario's "Run" button (rather than
just reacting to the 409) by polling `GET /api/benchmarks/current`. Completion is
detected by a fire-and-forget `asyncio` task awaiting the subprocess (same pattern
as the router's metric emission, Sprint 5), not by the requesting client staying
connected. `quality-failure` and `success` render their synthetic-data disclaimer
directly on the scenario card, not just buried in the eventual report.
