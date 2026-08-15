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

`precision`/`recall`/`false_positive_rate` are only computed over samples that
have a label — `null` (never `0`) when the window has zero labeled samples, a
real (possibly low) number once at least one exists. Labels arrive through the
delayed ingestion path described under [Policy engine](#policy-engine) below, not
by generating them locally — see [Known
limitations](../README.md#known-limitations).

`label_coverage` (`labeled_sample_count / sample_count`) follows the same
null-vs-zero convention as the metrics above: `null` only when `sample_count` is
itself zero (there's nothing to compute a fraction of), a real `0.0` once
predictions exist but none are labeled yet. `positive_label_count` is how many
of `labeled_sample_count` are the positive class (`actual_label=1`) — recall's
real denominator (`TP+FN`), not `labeled_sample_count` itself; see [Policy
engine](#policy-engine)'s `minimum_positive_labels` gate for why that
distinction matters. `label_delay_p50_seconds` / `label_delay_p95_seconds` are
percentiles of `label_ingested_at - created_at` over labeled samples only — how
long, in practice, ground truth takes to show up after a prediction is made.

p50/p95/p99 are computed in Python (`app/control_plane/metrics_service.py`), not
SQL — SQLite has no `percentile_cont` or window-function equivalent. The window
query pulls matching rows and computes linear-interpolation percentiles (same
convention as `numpy.percentile`'s default) over the in-memory list.

`compute_version_summary()` takes a `window_end_offset_seconds` parameter
(default `0`), shifting the whole window into the past rather than always ending
"now" — this is what lets the policy engine read two different slices of history
from the same function (reliability window vs. quality window, see [Policy
engine](#policy-engine)) without a parallel implementation.

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

## Label ingestion

`prediction_id` is minted exactly once, in `app/serving/` (a UUID4 returned on
every `/predict` response) — never regenerated by the router or the control
plane. It's the join key that lets a label, arriving independently and later,
be matched back to the exact prediction it grades.

`POST /api/labels` (and its batch sibling, `POST /api/labels/batch` —
`app/control_plane/labels_api.py`) is a separate surface from
`POST /api/deployments/{id}/metrics`: whoever generates a request is the only
party who can ever know its true label (the benchmark's Locust feeder reads it
straight from the labeled test-set row it just sent), and that knowledge
typically isn't available until well after the prediction itself. Idempotency is
part of the contract, not an afterthought: submitting the same
`(prediction_id, actual_label)` pair twice is a no-op (`200`); a *different*
`actual_label` for a `prediction_id` that already has one is a real conflict
(`409`, plus a `DeploymentEvent` audit row when a deployment is known) rather
than a silent overwrite — ground truth shouldn't quietly change underneath an
already-computed metric.

**`GroundTruthLabel`: always written, never coordinated at write time.**
Metric writes are deliberately fire-and-forget (`asyncio.create_task`, see
[Metrics](#metrics)) and label ingestion is a fully independent HTTP call —
nothing orders one before the other, and a label can legitimately arrive at
the control plane *before* the `PredictionMetric` row it belongs to has been
written. An earlier design (Sprint 12, retired in Sprint 14) parked an
early-arriving label in a separate `PendingLabel` table, consumed the moment
`metrics_service.record_metric` next saw a matching `prediction_id` — a
check-then-act coordination between two independent writers that turned out
to have a real, reproducible race (see [Desired/observed
reconciliation](#desiredobserved-reconciliation) for the exact interleaving
and the concurrency test that proves it's closed). `POST /api/labels` now
writes to `GroundTruthLabel` unconditionally, regardless of whether a
matching `PredictionMetric` exists yet — `record_metric` doesn't check
anything either, it's a plain `INSERT`. The two are only ever linked by a
read (`metrics_service.compute_version_summary`'s `PredictionMetric OUTER
JOIN GroundTruthLabel`), computed fresh on every quality-metrics read rather
than cached onto either row at write time. `202` from `POST /api/labels`
means "recorded, no matching metric yet" — advisory only, since nothing
polls or retries on the label's behalf; the next read that joins the two
tables picks it up whenever the metric lands.

`occurred_at` (label input, when the ground truth was actually observed) and
`ingested_at` (server timestamp, when the platform received it) are kept
distinct on `GroundTruthLabel` — `ingested_at - PredictionMetric.created_at`
is what `label_delay_p50_seconds`/`label_delay_p95_seconds` measure: how long
the platform's *pipeline* takes to learn a label, independent of how the
label producer chooses to report `occurred_at`. Both are persisted the
moment the row is created, regardless of arrival order.

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

**Two evaluation windows, not one.** Reliability checks (`minimum_requests`,
`latency_p95_increase`, `max_error_rate`) read `[now - window, now]` — the
freshest data available. Quality checks (`minimum_labeled_samples`,
`minimum_label_coverage`, `minimum_positive_labels`, `minimum_recall`) read an
older, *matured* slice instead: `[now - window - label_maturity_seconds, now -
label_maturity_seconds]`. This isn't an arbitrary choice — labels arrive
delayed (see [Label ingestion](#label-ingestion)), so the freshest window is,
by construction, the *least*-labeled one. Measuring recall there would keep an
otherwise-healthy canary permanently INCONCLUSIVE, since its newest
predictions never get a chance to accumulate ground truth before the window
moves on. Both windows are computed by the same
`compute_version_summary(..., window_end_offset_seconds=...)` call (see
[Metrics](#metrics)) with different offsets, not by two separate
implementations.

Before `minimum_recall` runs, three data-sufficiency checks must all pass:
`minimum_labeled_samples` (raw count of labeled predictions in the quality
window), `minimum_label_coverage` (that count as a fraction of the window's
total predictions), and `minimum_positive_labels` (how many of the labeled
predictions are the *positive* class, `actual_label=1`). This mirrors
`minimum_requests`'s own gate-then-proceed shape: if any check fails,
`minimum_recall` does **not** run at all — a canary with 100 predictions and
only 5 labeled must stay INCONCLUSIVE even if those 5 happen to have perfect
recall, because "not enough data to tell" and "looks good" are different
findings that must never collapse into each other.

**Why `minimum_labeled_samples`/`minimum_label_coverage` alone weren't
enough.** `recall = TP / (TP + FN)` — its denominator is *positive* examples,
not labeled examples in general. On a dataset with a low positive rate (this
project's fraud dataset is ~2%), a window can clear both of those checks
comfortably (say, 70+ labeled predictions, 90%+ coverage) while still
containing only 1-3 actual positives, at which point recall is a near-coin-flip,
not a measurement. **This was found live, not in a unit test**: an early version
of the CI quality scenarios (`ci_smoke_test.py`) let a genuinely healthy
canary (`v2-good`) get automatically rolled back purely because its quality
window happened to catch 1 positive example and miss it once, or catch 3 and
miss 2 — a real, reproducible bug this gate was added specifically to close.
Fixing it meant two things: (1) `minimum_positive_labels`, so the platform
itself refuses to trust a recall estimate built on too few positives, and
(2) making the CI scenarios that exercise this path send a *stratified*
request stream (`ci_smoke_test.py`'s `QUALITY_SCENARIO_POSITIVE_RATIO`) so
enough positives land in a short CI window deterministically — not a workaround
for the check, but a measurability requirement any short-lived, small-sample
test of a low-base-rate classifier has, real production traffic included. With
both fixes in place, scenarios 3 and 4 were verified across 5 consecutive
scenario-3/scenario-4 pairs (10 outcomes total), all correct.

**Stratification is sound for recall specifically — it would not be for every
metric.** Recall is base-rate independent: its denominator is only the actual
positives in the window, so raising the positive ratio changes which examples
land in the window but not the expected value of TP/(TP+FN) — it reduces
variance (fewer near-coin-flip windows) without shifting the estimate.
`precision` and `false_positive_rate` are the opposite: both mix in the
negative-class count in their denominator (`TP+FP` and `FP+TN` respectively),
so a stratified stream systematically changes their expected value, not just
its variance — computing them over `QUALITY_SCENARIO_POSITIVE_RATIO` traffic
would silently report numbers that don't reflect real, unstratified
production traffic. Both are already computed and returned by
`compute_version_summary` (see [Metrics](#metrics)) but neither is a policy
gate today. If either is ever promoted from a reported metric to a policy
gate, the CI traffic mix for whatever scenario exercises that gate must be
revisited first - stratification cannot simply be reused as-is.

The three data-sufficiency thresholds' current defaults (30 labeled samples,
50% coverage, 30 positive labels) are chosen for this project's demo scale, not
derived from a target statistical confidence level — a real system would size
`minimum_positive_labels` (and how long to wait for it) from the actual
positive-class rate and the confidence level a promotion decision needs, not a
flat constant.

`PolicyEvaluation` rows for quality checks snapshot the window and data they
actually used (`label_maturity_seconds`, `quality_window_start/end`,
`labeled_sample_count`, `label_coverage`, `positive_label_count`), for the same
audit-accuracy reason described below for `stable_weight`/`canary_weight`.

Overall verdict (`app/policy/engine.py::overall_result`): **FAIL beats
INCONCLUSIVE beats PASS**. One failing check fails the whole evaluation; an
inconclusive check can never be outvoted into a PASS by the other checks.
"Couldn't tell" and "looked fine" are never the same bucket.

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

## Manual automation hold

`Deployment.automation_paused` (a plain boolean, default `False`) is Kubernetes'
`spec.paused` / Argo Rollouts' manual pause, for the same reason: a real control
plane needs a way to say "don't let the automated actor touch this one" that
doesn't depend on timing. `app/worker/loop.py`'s `run_once` filters paused
deployments out of `active_ids` before anything else runs - a paused deployment
produces **zero** worker-originated API calls, not just a skipped action.
`POST /api/deployments/{id}/pause-automation` / `/resume-automation` flip the
flag (409 on an already-terminal deployment, same guard as everywhere else);
`POST /api/deployments` also accepts `automation_paused: true` at creation time,
for a caller that wants to drive a deployment purely manually from the start
without a create-then-pause round trip that would leave a real window for the
worker to already have acted.

**This was not a speculative feature - it's what closed a real, reproducible CI
bug.** `scripts/ci_smoke_test.py`'s scenario 1 ("manual create -> evaluate ->
promote") is a purely manual flow by design: it calls `/evaluate` and
`/promote` itself, deliberately not waiting on the worker, using the
platform's default (non-generous) `latency_p95_increase` threshold (20%) since
proving the manual CRUD surface works isn't about latency at all. But the
worker sweeps **every** `CANARY_RUNNING`/`EVALUATING` deployment on every poll
cycle, regardless of which test scenario created it - there was no way to tell
it "leave this one alone." On three separate, otherwise-unrelated commits (none
of which touched this scenario's code - confirmed by diffing across them), the
GitHub Actions runner's real inference-latency noise between `v1`/`v2-good`
occasionally crossed that 20% threshold before the scenario's own manual
`/evaluate` call ran, so the worker's own first sweep saw a genuine
`latency_p95_increase` FAIL, called `/rollback` itself, and the scenario's
manual `/evaluate` then 409'd against an already-`ROLLED_BACK` deployment. Five
days of the exact same test code passing, then failing identically across
unrelated commits, is what a missing control-plane primitive looks like, not a
flaky test - the fix is a feature the worker's design was always missing, not a
longer sleep or a loosened threshold. Scenario 1 now creates its deployment
with `automation_paused=True`, which both fixes the race and is itself a live
proof the hold works: the scenario asserts an `automation_paused`
`DeploymentEvent` appears on the deployment's timeline.

**Why the "automation paused" `DeploymentEvent` is written once, at the moment
the flag flips - not by the worker noticing it on some later sweep.** The
worker is deliberately stateless (see above: no in-memory rollout state,
everything re-derived every cycle) - giving it "have I already logged a skip
for this one" bookkeeping would mean either a new API call on every skipped
sweep (event-log spam - the flag doesn't change between sweeps, there's nothing
new to say) or genuine in-memory state the design elsewhere goes out of its way
not to need. Instead, `pause_automation`/`resume_automation` and
`create_deployment` (when `automation_paused=True`) write the event themselves,
synchronously, in the same transaction that flips the flag - exactly once, no
matter how many sweeps later the worker happens to notice. `pause_automation`/
`resume_automation` are also idempotent (a repeat call on an
already-paused/already-resumed deployment is a silent no-op, not a duplicate
event or an error) for the same reason: a double-click or a retried request
must not spam the timeline.

Manual `/evaluate`, `/promote`, `/rollback` are completely unaffected by the
hold - it only ever stops the *automated* actor. An operator can always act,
paused or not; the hold exists so a human inspecting a deployment doesn't have
the worker pull the rug out from under them mid-review, and so a script that
wants to drive a deployment by hand doesn't have to win a race against the
worker to do it.

**Scenarios 2-4 don't need the hold - they still race the worker on purpose.**
Scenario 2 injects a real +400ms fault against the platform default 20%
threshold (400ms is far beyond any inference-time noise this project has
observed) - a genuine, tight latency check is exactly what that scenario is
testing. Scenario 3 and 4 set the latency threshold high enough to disable the
latency check in practice (`latency_p95_increase: 2000%`, chosen specifically
during this investigation - the largest real latency variance observed
anywhere in this project, per `scripts/benchmarks/scenarios.py`'s own notes on
this exact false-positive class, was under 5x), so the promote/rollback
decision in those two scenarios rests solely on the quality signal
(`minimum_recall`), not on latency at all. Latency-driven rollback is covered
separately, and for real, by scenario 2 - scenarios 3/4 have nothing further
to prove there and would only add CI flakiness by trying.

## Desired/observed reconciliation

**The bug this closes.** Every traffic-changing action (`create_deployment`,
`promote_deployment`, `rollback_deployment`, `advance_traffic`) used to push the
new config to the router *before* committing it to the DB. Two concurrent
requests against the same deployment (a human promoting while the worker's own
FAIL decision rolls back, say) could both read the same `version_id`, both push
to the router, and only one would win the DB's optimistic lock - but the loser
had *already written to the router* by the time its commit was rejected. Result:
the DB says PROMOTED, the router is still serving 100% stable. No data was
corrupted, but the control plane was now lying about what was actually running.
A second, unrelated version of the same class of bug: the router keeps its
traffic split in memory only, so a plain restart loses it and nothing notices.

**The fix has two parts, and neither alone is sufficient.** (1) Commit desired
state to the DB *first*, push to the router *after* - see
`service._push_best_effort` and every action function's comment on this. This
closes the original race outright: a losing writer's commit now fails at the DB
layer before it ever attempts a router push, so the router can no longer end up
holding a stale writer's state. But reversing the order introduces a new,
narrower gap on its own: a commit can succeed and the *following* push can then
fail (router down, network blip) or land stale, leaving DB (desired) and router
(observed) briefly diverged with nothing to close that gap. (2) The reconciler
(`app/control_plane/reconcile.py`) is what actually guarantees convergence,
closing exactly that gap on a schedule - which is why this is shipped as one
change, not two: commit-then-push without a reconciler would just trade "the
router can lie" for "the router can silently drift and stay wrong forever."

**No outbox table.** The desired state a reconciler needs to compare against is
already fully durable in `Deployment`/`TrafficAllocation` - that's the whole
point of committing before pushing. An outbox table (a queue of "pending router
pushes" rows) would store the *same* information a second time, in a second
place that itself needs to stay consistent with the first - a new source of the
exact kind of bug this feature exists to remove, for no benefit: nothing here
needs ordered delivery, exactly-once semantics, or a producer/consumer split
across process boundaries. The reconciler doesn't drain a queue; it diffs two
already-durable pieces of state and re-pushes when they disagree, exactly like
a Kubernetes controller diffs a resource's `spec` against its live status
rather than replaying a log of past intents.

**Revision scope: model-scoped as of Sprint 14, not per-deployment.**
`TrafficAllocation.revision` originally (Sprint 13) incremented once per
`targets` change, scoped to the one deployment row it lives on - reasoned as
follows: `uq_deployments_active_per_model` (see [Control plane & deployment
lifecycle](#control-plane--deployment-lifecycle)) guarantees at most one
non-terminal deployment per model at any time, so a different `deployment_id`
is definitionally a different rollout and can just always win outright,
regardless of what revision number it starts from - no need for a model-scoped
counter, which would've meant adding a table this project otherwise had no use
for (models are plain strings, no `Model` table). That reasoning quietly broke
the moment a *terminal* deployment's allocation could stay authoritative (see
"Terminal-state reconciliation" below): once "a different deployment_id always
wins" is the rule, a push delayed enough to arrive after a *newer* deployment
had already superseded it - a late reconcile tick, a slow network path, a
retried request - could silently resurrect stale traffic, since nothing
compared it against what the model's current deployment actually was.
Sprint 14 fixes this at the root: `RoutingGeneration` (one row per
`model_name`, `service._next_routing_generation`) is a genuine model-scoped
monotonic counter now, and `TrafficAllocation.revision` is stamped from it on
every write, regardless of which deployment_id is writing. The table this
avoided in Sprint 13 turned out to be necessary after all, once the feature it
was avoided for (authoritative-after-terminal) existed - not a mistake in the
original call, just a call whose premise changed.

**A stale-revision 409 is coordination working, not a failure.** `app/router/
main.py`'s `put_config` rejects a push for the same *model* (not
`deployment_id` - see the revision-scope note above) whose revision isn't
strictly greater than what it already has - see `StaleRevisionError`. This
fires constantly in entirely healthy operation: the losing side of a race, or
a reconcile tick that lost a footrace against a concurrent promote/rollback
that had already landed the same or a newer revision by the time the
reconciler's own push arrived. `service._push_best_effort` catches it and logs
at `info`, not `warning` or `error`, and never touches the deployment's status
- see the next paragraph for why a plain `RouterUpdateError` (the router being
genuinely unreachable) is treated the same way for a different reason. Both
are deliberately distinct exception types, though, because they mean different
things to a human reading logs: one says "someone else's write already won,"
the other says "the router didn't respond at all."

**Why a router push failure no longer marks a deployment FAILED.** Before this
sprint, an unreachable router during promote/rollback/advance/create
transitioned the deployment to `FAILED`. That made sense when the push
happened *before* the commit - if the push failed, nothing had happened yet,
so `FAILED` was accurate. It stopped being accurate once desired state commits
first: by the time a push can fail, the DB has *already* agreed that
`CANARY_RUNNING`/`PROMOTED`/`ROLLED_BACK`/the new traffic split is this
deployment's real, current, intended state. Marking it `FAILED` at that point
wouldn't describe reality - it would silently discard a decision that was
already made and durably recorded, over a problem (the router hasn't caught up
yet) the reconciler exists specifically to fix on its own. `FAILED` remains a
valid state-machine target (see `state_machine.py`) for genuine irrecoverable
failures; it's just no longer what a temporarily-unreachable router produces.

**A router outage must be visible, not just swallowed.** Not transitioning to
`FAILED` is only defensible because the outage doesn't disappear - it shows up
somewhere a human can see it. `service.record_router_reachability_change`
writes a one-time `router_unreachable`/`router_recovered` `DeploymentEvent` on
the actual transition (derived from the deployment's own event history, not a
new column - see the function's docstring), called from both
`_push_best_effort` and `reconcile.py`, so a sustained outage produces exactly
one event on the way down and one on the way back up, not a log line per tick
that nothing else ever surfaces. `GET /api/router/observed` reports a
`reachable` flag for the same reason: a genuinely unreachable router and a
reachable-but-never-configured one (e.g. right after a restart) both report
`deployment_id: None`, so without `reachable` the dashboard couldn't tell a
serious outage apart from a harmless one-tick-old boot state - which would
make the drift warning disappear exactly when it matters most.

**Terminal-state reconciliation (Sprint 14).** The reconciler and the
router's startup sync (`GET /api/router-config/{model_name}`) both used to
compare only against `service.get_active_deployment`
(`CANARY_RUNNING`/`EVALUATING`) - itself a direct consequence of "commit
desired state first," which was designed around a rollout still being *in
flight* when the push after it fails. The gap: `promote_deployment`/
`rollback_deployment`'s commit moves the deployment to `PROMOTED`/
`ROLLED_BACK` in that same commit, so by the time a router push after it can
fail, the deployment has already left `ACTIVE_STATUSES` - and
`get_active_deployment` then finds nothing at all. A router restart, or a
push failure, at exactly that moment left the drift permanent instead of
closing on the next reconcile tick, and README's own "even after the router
restarts" claim was quietly false for this one case. `service.
get_authoritative_allocation` is the fix: it checks, in order, (1) an
in-flight rollout, or a rollout `record_inconclusive` has frozen into
`INCONCLUSIVE` - grouped together because both leave the deployment's
`TrafficAllocation` as the correct desired routing state, and
`uq_deployments_active_per_model` already guarantees at most one of the two
exists per model at a time - and (2) otherwise, the most recent `PROMOTED`/
`ROLLED_BACK` deployment's *final* allocation. `INCONCLUSIVE` was added to
step 1 in a follow-up fix after the first version of this function shipped
without it: `record_inconclusive`'s own contract is "freeze the traffic
split for manual review", and a deployment leaving `CANARY_RUNNING`/
`EVALUATING` into `INCONCLUSIVE` is exactly the same kind of "no longer
active but still authoritative" transition `PROMOTED`/`ROLLED_BACK` already
were - a reconcile tick that only knew about the terminal pair could still
silently revert a frozen rollout's traffic back to whatever the *previous*
deployment had running, which is precisely the kind of drift this feature
exists to prevent.

**Why `get_authoritative_allocation` is a separate function, not a widened
`get_active_deployment`.** `get_active_deployment` backs two things whose
correctness depends on its exact, narrow scope: the exclusivity pre-check in
`create_deployment` (a new deployment is blocked only while one is genuinely
in flight - blocking on a terminal or frozen deployment too would make the
model permanently unstartable) and the automation hold's `require_active`
guard. Widening what "active" means for one caller's sake would silently
widen it for both of those too. Two functions, two questions -
`get_active_deployment` asks "is this deployment active from automation's
point of view", `get_authoritative_allocation` asks "what should the router
be serving" - kept answerable independently.

**Why `FAILED` is excluded from the authoritative fallback.** A deployment
that reaches `FAILED` never completed a legitimate rollout decision - unlike
`PROMOTED`/`ROLLED_BACK`/`INCONCLUSIVE`, its `TrafficAllocation` doesn't
represent something the platform actually decided was correct, just whatever
traffic split happened to exist at the moment something broke. Treating it
as authoritative would mean a router restart could sync to an accidental,
half-finished split rather than the *last deployment that actually reached a
real outcome* (or, if none exists yet, the router's own bootstrap default) -
falling further back is more honest than trusting a failure's leftover
state.

**Reachability events belong to whichever deployment owns the routing
state.** `service.record_router_reachability_change`'s caller always
supplies a `deployment_id` directly, but there are two places that first
have to *pick* one on their own: `reconcile.py`'s router-unreachable branch
(the `GET` itself failed, so there's no observed `model_name` to look a
specific deployment up by) had used a plain "every currently-active
deployment" query - the same `ACTIVE_STATUSES`-only gap `get_authoritative_
allocation` was created to close for routing state itself, just recreated
independently for reachability events. A router outage while the
authoritative deployment for a model was terminal or frozen `INCONCLUSIVE`
produced no visible event at all on that deployment's timeline, even though
the outage affected exactly the traffic split it owns. Fixed by routing
`_all_authoritative_deployments` through `get_authoritative_allocation`
itself (once per distinct `model_name`) instead of maintaining a second,
narrower definition of "which deployment does this router state belong to" -
one function answers that question everywhere it's asked. The one-event-
per-transition rule is unchanged.

**Durable ground-truth labels (Sprint 14).** Label ingestion
(`label_service.ingest_label`, via `POST /api/labels`) and metric ingestion
(`metrics_service.record_metric`, via the router's fire-and-forget metric
push) used to be two independent check-then-act writers coordinating through
a third table, `PendingLabel`: a label arriving before its metric parked
itself there, consumed the moment `record_metric` next saw a matching
`prediction_id`. That "moment" was the bug - a real, reproducible race: label
transaction checks for the metric (not found, since the metric transaction
hasn't committed), metric transaction inserts the metric, label transaction
checks `PendingLabel` for itself (not found, first time), inserts a
`PendingLabel` row, metric transaction checks `PendingLabel` for a match (not
found - the label transaction's insert isn't committed yet), both commit.
Result: a `PredictionMetric` with no label, and a `PendingLabel` row nothing
would ever consume again, since the one and only place that checked for a
match (`record_metric`) had already run. Sequential arrival-order tests
(label-then-metric, metric-then-label) can't catch this - neither exercises a
genuinely uncommitted overlap between the two transactions.

The fix removes the coordination problem instead of trying to out-time it:
`GroundTruthLabel` is written unconditionally on every ingestion, with no
check against `PredictionMetric` at all - `record_metric` doesn't check
anything either now, it's a plain `INSERT`. The two are only ever linked by a
read, `metrics_service.compute_version_summary`'s `PredictionMetric OUTER
JOIN GroundTruthLabel ON prediction_id`, computed fresh every time quality
metrics are read rather than cached onto either row at write time. Since
neither write depends on the other's existence, there is no interleaving that
can leave them unlinked - correctness holds by construction, not by winning a
timing race, which is also why the concurrency test proving this
(`test_concurrent_label_and_metric_writes_always_end_up_joined`) can
deliberately reconstruct the exact interleaving that broke the old design and
still expect it to pass. `occurred_at` is now always persisted regardless of
arrival order too, for the same reason: it lives on `GroundTruthLabel` from
the moment that row is created, not copied onto `PredictionMetric` only when
convenient.

`POST /api/labels` returning `202` no longer means "parked, waiting to be
matched" - it means "recorded, no matching `PredictionMetric` yet," which is
purely advisory (nothing polls or retries on the label's behalf; the next
read that joins the two tables picks it up whenever the metric lands, with no
special handling required). The idempotency/conflict semantics
(same-value-twice is a no-op, different-value is a `409` + audit event when a
deployment is known) are unchanged in meaning, just re-anchored to
`GroundTruthLabel`'s own unique constraint on `prediction_id` instead of two
separate tables' worth of checks.

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
