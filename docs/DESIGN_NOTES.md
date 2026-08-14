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

**One active deployment per model - enforced at two layers, not check-then-act.**
`POST /api/deployments` 409s (`ActiveDeploymentExistsError`) if `model_name`
already has a non-terminal deployment — regardless of `Idempotency-Key`, which
only dedupes a *retry of the same logical request*, not two genuinely different
requests for the same model. Without this, two concurrent rollouts for one model
would silently fight over the router's single traffic-split slot
(`RouterConfigStore`, Sprint 3) with no way to tell which one "wins." A caller
that wants to replace an in-flight rollout has to promote or roll it back first,
same as a human would.

The app-level pre-check (`service.get_active_deployment`, querying
`CANARY_RUNNING`/`EVALUATING` only) exists purely for a friendly, immediate error
message in the common case - on its own it's check-then-act and races: two
concurrent requests can both read "no active deployment" before either commits.
The real guarantee is `uq_deployments_active_per_model`, a **partial unique
index** on `deployments.model_name` (see the migration that added it,
`Deployment.__table_args__`, and `sqlite_where`/`postgresql_where` both being set
so an eventual Postgres move doesn't need this index rewritten) covering every
*non-terminal* status - deliberately wider than the pre-check's
`CANARY_RUNNING`/`EVALUATING`: `INCONCLUSIVE` counts too, since a frozen,
unresolved deployment is just as much "not done" as a running one. A request that
slips past the pre-check still hits this index at `flush()` time; `service.
create_deployment` catches that `IntegrityError` and translates it into the same
`ActiveDeploymentExistsError` the pre-check raises (distinguishing it from an
`idempotency_key` collision on the same statement, a different constraint with a
different meaning) - so a caller never needs to know two mechanisms exist, only
that the guarantee is real either way.

`POST /api/deployments/{id}/evaluate` (see [Policy engine](#policy-engine)) has
the same active-only requirement as the worker's own action endpoints — a
`PROMOTED`/`ROLLED_BACK`/`FAILED`/`INCONCLUSIVE` deployment has nothing left to
evaluate, and recording more `PolicyEvaluation` rows against it would just
misrepresent the timeline as if the rollout were still being judged.

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

**Explanations are audit-accurate, not present-tense.** Each `PolicyEvaluation`
row snapshots the deployment's own context *at the moment it ran*
(`evaluation_window_seconds`, `stable_weight`, `canary_weight`,
`stable_sample_count`, `canary_sample_count` — all nullable, since rows written
before this snapshot existed have none). `app/policy/explain.py`'s human-readable
`explanation` (surfaced on the timeline, see [Incident timeline & explainable
policy UI](../README.md#incident-timeline--explainable-policy-ui-sprint-10)) uses
that snapshot, not the deployment's *current* traffic split - otherwise an old
`minimum_requests` INCONCLUSIVE would silently reword itself every time the
deployment's traffic changed later, misrepresenting what was actually true when
the check fired. Rows with no snapshot (pre-migration) fall back to current state
and say so explicitly (`is_estimated: true` in the API response, plus a note in
the prose) rather than presenting a guess as recorded fact.

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

**Race safety, two layers**: every action endpoint (`advance-traffic`, `promote`,
`rollback`, `record-inconclusive`) independently re-checks the deployment is still
`CANARY_RUNNING`/`EVALUATING` server-side and returns **409** otherwise - this
catches a *sequential* stale action (the deployment settled into a new status
before this request even started, e.g. a human acted between the worker's
`evaluate` call and its follow-up action).

That status check alone is not enough for a genuinely *concurrent* race, though:
two requests can both read the same "still active" status and both pass every
in-memory check before either commits - `Deployment.status` doesn't change during
`promote_deployment`/`rollback_deployment`'s in-memory transitions until the final
`db.commit()`. `Deployment.version_id` (a SQLAlchemy `version_id_col`, bumped on
every commit that mutates the row - see `service._touch`, needed because
`advance_traffic`'s success path otherwise only touches `TrafficAllocation`, a
different table) closes this: a commit whose `WHERE version_id = <what this
session read>` matches zero rows raises `StaleDataError`, which `service._commit`
turns into `ConcurrentUpdateError` (409). Whichever request commits first wins;
the loser gets a clean 409 instead of silently overwriting - or racing to
corrupt - what the winner just wrote. The worker treats any 409 (stale-status or
concurrent-update) as "someone else already handled this," logs it, and moves on
to the next deployment.

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

## Future vision

Features that were thought through but deliberately left out - not forgotten, not
half-built, just not worth the time/complexity trade-off for a project whose goal
is demonstrating the core canary-rollout loop clearly, not being a complete
platform. Listed here instead of as TODOs scattered through the code so the
reasoning survives even if nobody's actively looking at this file.

- **Multi-model support.** Every service today assumes one logical model
  (`fraud-model`) with several versions. Nothing in the schema *requires* that -
  `Deployment.model_name` is already a free-text field, and the router already
  keys `ROUTER_VERSION_HOSTS` per model - but the dashboard has no model picker,
  and nothing stops two deployments for different models from racing over the
  same router config slot (see `RouterConfigStore`'s single-slot design, Sprint 3).
  Supporting this for real means the router holding one active config *per model*,
  not one globally - a real change to a component that's otherwise been stable
  since Sprint 3, not worth destabilizing for a demo with one model.
- **A real inference gateway** (request validation independent of each serving
  container's dynamic schema, response caching, batching, rate limiting,
  authentication on `/predict` itself). Today the router is deliberately dumb - it
  forwards bodies unmodified and lets each serving container's own pydantic model
  reject bad input (see [Traffic router](#traffic-router) above). A gateway is a
  meaningfully different component with its own request lifecycle and its own
  failure modes; bolting gateway concerns onto the router would blur the one job
  it has now (pick a target, forward, don't lie about failures).
- **Cost, drift, data-quality, and availability policies.** The policy engine's
  four checks (Sprint 7) only ever asked "is this canary statistically safe right
  now, from the traffic it's already seeing?" Cost ($/inference), drift (feature
  distribution shift vs. training data), data quality (schema violations, null
  rates, out-of-range values arriving at `/predict`), and availability (uptime
  SLO tracking over time, not just point-in-time error rate) are each a real
  input to a real promotion decision - but each needs its own data source this
  project doesn't have (a billing feed, a reference training distribution stored
  per model version, a schema registry, a longer-horizon metrics store), not just
  another `PolicyCheckResult` case. Adding the case without the data source behind
  it would be exactly the kind of "looks done, isn't" the rest of this project
  tries hard to avoid (see `minimum_recall`'s honest `INCONCLUSIVE` instead of a
  fake `PASS`, for the same reason).
- **A model approval workflow** (a human sign-off gate before a deployment can
  even start, separate from the promote/rollback decision once it's running).
  Today anyone who can call `POST /api/deployments` can start a canary - there's
  no draft/pending-approval state in the state machine, and adding one changes the
  state machine itself (Sprint 4), which every other component (worker, dashboard,
  benchmark suite) already depends on being stable. Worth doing before any real
  usage; not worth the churn for a demo where the "who's allowed to deploy"
  question doesn't have a real answer anyway (see [Production
  evolution](../README.md#production-evolution)'s auth row).
- **Environment separation** (dev/staging/prod as first-class concepts, each with
  its own policy thresholds, its own router/serving fleet, promotion *between*
  environments rather than just traffic stages within one). Everything here - one
  `docker-compose.yml`, one `PolicySettings`, one SQLite file - is implicitly
  "one environment." Modeling more than one honestly needs separate
  infrastructure per environment (see [Production
  evolution](../README.md#production-evolution)'s Kubernetes row) more than it
  needs new application code; simulating it with a flag inside this single-stack
  demo would misrepresent what real environment isolation actually requires.
