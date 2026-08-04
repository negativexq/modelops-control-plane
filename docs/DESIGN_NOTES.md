# Design notes

The rationale behind decisions in [README.md](../README.md), grouped by the same
components. This file exists so the top-level README can stay skimmable without
losing the "why" behind anything non-obvious.

## Model serving

Fault injection (`INJECTED_LATENCY_MS`/`INJECTED_ERROR_RATE` env vars, or the
runtime `PUT /fault-injection`) is forced off whenever `ENVIRONMENT=production`,
regardless of what it's set to - the guard is re-checked on every runtime mutation,
not just at process startup, since a `BaseSettings` object is otherwise only
validated once at construction. This is what lets the benchmark suite toggle fault
injection on a long-lived container over plain HTTP instead of restarting it, while
guaranteeing the same container can never be tricked into injecting faults in
production.

## Traffic router

`app.router.main:app` reads *where* each version is running from its own static
config (`ROUTER_VERSION_HOSTS`) — never from the model registry, and never from the
control plane, which only ever sends `"version -> weight"`. This keeps the control
plane from ever needing to know a version's host/port, and lets a benchmark point a
version label at a "broken" fault-injection instance without touching the real one
(the router already treats a version label as independent from what a serving
container calls itself internally).

Safety behavior:

- If the target picked by the weighted draw is not `/ready`, or is unreachable, the
  router returns **503 and logs a warning** — it does **not** silently fall back to
  another target. Silent failover would let a broken canary hide behind another
  target's success rate, defeating the point of a canary rollout.
- Upstream calls use a **5s timeout and no retries**
  (`ROUTER_UPSTREAM_TIMEOUT_SECONDS`). This is deliberate: retries would mask the
  transient failures a promotion decision needs to see, and could double-apply side
  effects on the model service. A caller of the router that wants retries should add
  them explicitly on its own side.

## Control plane & deployment lifecycle

`promote`/`rollback` auto-advance `CANARY_RUNNING -> EVALUATING` first if needed,
then require `EVALUATING` or `INCONCLUSIVE`.

The control plane never resolves or sends a version's host/port to the router —
only `{version, weight}` pairs (`app/control_plane/router_gateway.py`). If the
router push fails, the deployment is marked `FAILED` (with the reason logged as an
event) rather than left in an ambiguous in-flight state.

`RouterGateway` reuses a single `httpx.AsyncClient` created once in `app.main`'s
lifespan (`app.state.http_client`) rather than opening/closing one per request.

`GET /api/router-config/{model_name}` is the router's startup-sync source: the
currently-**active** allocation for that model (status `CANARY_RUNNING` or
`EVALUATING`) — a `FAILED`/`ROLLED_BACK`/`PROMOTED` deployment is never returned
here even if it's the most recent row.

## Metrics

`POST /api/deployments/{id}/metrics` is deliberately minimal (one PK existence
check, one insert, no joins, no state-machine/event-log involvement) since it's a
hot path called after every router forward.

How the router publishes without slowing `/predict` down: after forwarding and
building the response, it schedules `asyncio.create_task(send_metric(...))` and
returns immediately — it does **not** use FastAPI's `BackgroundTasks`, because
Starlette awaits those as part of the same ASGI response cycle (so a slow metrics
call would still show up as added latency to anything measuring end-to-end). A bare
`create_task` is genuinely decoupled: the response is already on its way to the
client before that task runs. Task references are kept in a small set with a
done-callback (`app/router/main.py`) purely so asyncio doesn't warn about a
garbage-collected pending task — nothing awaits them as part of request handling.
If the metric POST fails or the control plane is unreachable, it's caught and
logged inside `send_metric` (`app/router/metrics.py`); `/predict` already returned
its result by then regardless.

`precision`/`recall`/`false_positive_rate` are only computed for samples where
`actual_label` has been backfilled — see [Known
limitations](../README.md#known-limitations).

p50/p95/p99 are computed in Python (`app/control_plane/metrics_service.py`), not
SQL — SQLite has no `percentile_cont` or window-function equivalent. The window
query pulls matching rows and computes linear-interpolation percentiles (same
convention as `numpy.percentile`'s default) over the in-memory list.

## Dashboard

The frontend calls the control plane's REST API directly from the browser — no
Next.js server-side data fetching, since every page needs an explicit,
non-optimistic refetch after mutations.

- `lib/useAsync.ts` — fetch-on-mount-and-deps-change with an explicit `refetch()`.
  Deliberately no cache/optimistic layer.
- `lib/useMutation.ts` — wraps promote/rollback/create-deployment/start-benchmark
  calls; exposes `submitting` + a `{kind, message}` `result`, shown **after the
  real API response comes back**, then the caller's `refetch()` re-reads actual
  state. There's no local "assume it worked" update anywhere.
- `lib/format.ts` — `formatNumber`/`formatMs`/`formatPercent` render `null` as
  **"N/A"**, never "0" — load-bearing for the comparison view, since
  precision/recall/false-positive-rate are `null` (not zero) until something
  backfills `actual_label`. `parseApiDate` treats an offset-less ISO timestamp as
  UTC before parsing — the same SQLite `DateTime(timezone=True)`-isn't-enforced
  issue shows up on the client side as `new Date(...)` otherwise silently
  misinterpreting a UTC timestamp as local time.

## Policy engine

`app/policy/` turns a comparison window into a PASS/FAIL/INCONCLUSIVE verdict, per
policy, persisted as an audit trail — it does **not** touch deployment state
(that's the worker's job; promote/rollback are still separate API calls).

`PolicyConfig` is a plain Pydantic model, with defaults from `PolicySettings` (env
vars prefixed `POLICY_`). Once a deployment is created, its resolved
`PolicyConfig` is persisted on the row — `evaluation_window_seconds` is part of
*that* policy, not a query param like the comparison endpoint's `window_seconds`,
because a policy's verdict has to be reproducible from the policy alone, not from
whatever window happens to be passed at evaluation time.

If `minimum_requests` isn't met by *either* version, that's the **only** check
that runs — the other three never evaluate against too little traffic.

Overall verdict (`app/policy/engine.py::overall_result`): **FAIL beats
INCONCLUSIVE beats PASS**. One failing check fails the whole evaluation; an
inconclusive check (most often `minimum_recall`, since no `actual_label` source
exists) can never be outvoted into a PASS by the other checks. "Couldn't tell" and
"looked fine" are never the same bucket.

## Automated promotion & rollback

The worker has **no direct DB access and keeps no in-memory rollout state**; every
decision is re-derived from what `GET /api/deployments/{id}` and `GET
.../policy-evaluations` report *right now*, so restarting it just resumes.

The next traffic stage is always "the smallest stage weight above the canary's
*current* weight" read from its live `TrafficAllocation`
(`control_plane/service.py::TRAFFIC_STAGES`) — not a stored index — so it's correct
regardless of what custom `canary_weight` the deployment started at, and survives a
worker restart with zero extra bookkeeping.

**Dedup**: the worker doesn't just sleep-and-poll `evaluation_window_seconds` - it
checks the deployment's *own* most recent `PolicyEvaluation.evaluated_at` and skips
re-evaluating until that policy's window has actually elapsed. This is what makes
it restart-safe without a separate "last checked" store: the record it needs
already exists in the same table being written to.

**Race safety**: every action endpoint (`advance-traffic`, `promote`, `rollback`,
`record-inconclusive`) independently re-checks the deployment is still
`CANARY_RUNNING`/`EVALUATING` server-side and returns **409** otherwise - so if a
human promotes/rolls back a deployment between the worker's `evaluate` call and its
follow-up action, the server rejects the stale action instead of corrupting an
already-final `TrafficAllocation`. The worker treats a 409 as "someone else already
handled this," logs it, and moves on to the next deployment.

## Benchmark suite

Locust was chosen over k6 because it's Python, so the load definition
(`locustfile.py`) and the orchestration (creating deployments, polling outcomes,
computing timings) live in the same language and process family as the rest of
`backend/scripts/`, with no separate JS toolchain. Locust runs as a subprocess in
headless mode (`--csv=...`); the orchestrator parses its stats CSV rather than
embedding Locust's gevent runtime in-process (mixing gevent with the rest of the
codebase's asyncio/httpx would be its own source of bugs).

**Isolation**: each scenario gets its own `model_name`, so its
`Deployment`/`PolicyEvaluation`/`DeploymentEvent` rows never mix with a real
deployment created from the dashboard. This does **not** extend to the router: it
holds exactly one active traffic split, so running a benchmark concurrently with a
real demo will overwrite that demo's traffic split for the benchmark's duration.

**Fault injection without a Docker socket**: the two fault-injected scenarios
target dedicated containers that serve the *same* v2-good artifact as the real
`model-serving-v2-good`. These containers are **always up** as part of the normal
stack, fault injection **off by default** — there is no container start/stop step.
The orchestrator calls `PUT /fault-injection` on the relevant container over plain
HTTP before generating load, and always resets it back to zero in a `finally`
block afterward — even if the run crashes — so a failed or interrupted benchmark
can never leave fault injection on for the next run or for real traffic. No Docker
socket or container lifecycle access is needed anywhere in this path; giving the
backend container that kind of host access was considered and deliberately
rejected as an unacceptable amount of privilege for what it's used for.

Each run's timing numbers naturally land around `poll_interval_seconds +
evaluation_window_seconds` (worker default: 15s poll interval; each scenario sets
its own, short `evaluation_window_seconds`) — that's the real, expected latency of
the automation loop, not a benchmark failure to explain away.

**Dashboard-triggered runs**: `POST /api/benchmarks/run` spawns
`python -m scripts.benchmarks.run_benchmark` as a subprocess of the backend itself
(the `scripts/` directory ships inside the backend image, and `locust` is a core
dependency there, not just a dev one) and tracks it in a `BenchmarkRun` row.
Completion is detected by a fire-and-forget `asyncio` task awaiting the subprocess
(same pattern as the router's metric emission), not by the requesting client
staying connected — the same reasoning as the metrics hot path above.
