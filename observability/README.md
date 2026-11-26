# Observability

`docker compose up` starts three more containers next to the ModelOps services:

| | URL | What it's for |
|---|---|---|
| Grafana | http://localhost:3001 | Dashboards. Opens on **ModelOps · Rollouts**, no login. |
| Prometheus | http://localhost:9090 | Scrapes every service every 5s, evaluates the alert rules. |
| Jaeger | http://localhost:16686 | Traces, one per prediction and one per worker sweep. |

## Dashboards

### ModelOps · Rollouts (home)

The question it answers: *is the canary healthy, and what is the control loop
doing about it?*

- **Headline row.** Predictions/s, error rate, p95 latency, live rollouts,
  decisions in the selected range (promoted / rolled back / inconclusive) and
  the worker heartbeat (seconds since its last sweep; red means rollouts aren't
  being evaluated). Under it, any firing Prometheus alert.
- **Canary vs stable, one section per model** (the `Model` variable; Grafana
  repeats the section for each selected model):
  - the traffic split as the router applied it, stepping with each rollout stage;
  - requests, p95/p50 latency and error rate per version, with the alert
    thresholds drawn in (500 ms, 5%);
  - a latency heatmap, where a slow canary shows up as a second band;
  - responses by status.

  Clicking a series opens that version's traces in Jaeger.
- **Control loop.** Decisions (automatic vs manual), policy verdicts, which
  gates fail most, worker actions, sweep duration, and reconcile results
  (`corrected` = the reconciler repaired drift, red = it couldn't reach the
  router).

Rollout decisions are drawn as markers on every time series (toggle "Rollout
decisions" at the top).

### ModelOps · Services

Rate, errors and duration for each service: up/down tiles for every scrape
target, requests and p95 per service, a table of routes sorted by p95 (last
5 minutes), and per-container model inference time.

Inference time next to router latency separates the two kinds of slowness. If
router p95 climbs while inference stays flat, the time is going to the network
or an injected fault, not the model.

## Metrics

Every HTTP service exposes `GET /metrics`. The worker has no web app, so it
serves the same format on `:9100`. All names start with `modelops_`.

| Metric | Labels | From |
|---|---|---|
| `http_requests_total`, `http_request_duration_seconds` | service, method, route (template, e.g. `/api/deployments/{deployment_id}`), status | backend, router, serving |
| `router_requests_total` | model, version, status | router |
| `router_upstream_latency_seconds` | model, version | router |
| `router_traffic_weight`, `router_config_revision` | model, version / model | router |
| `router_breaker_state` (0 closed, 1 half-open, 2 open), `router_rate_limited_total` | version / model | router |
| `serving_inference_seconds` | model, version | model serving |
| `decisions_total` | outcome, trigger (automatic/manual) | backend |
| `policy_evaluations_total`, `policy_checks_total` | result / policy, result | backend |
| `reconcile_runs_total`, `reconcile_errors_total` | model, result / reason | backend |
| `router_push_failures_total` | model | backend |
| `worker_sweeps_total`, `worker_sweep_duration_seconds`, `worker_last_sweep_timestamp_seconds`, `worker_active_rollouts` | | worker |
| `worker_actions_total`, `worker_errors_total` | action / stage | worker |

Counters live in each process and restart at zero with it. The panels use
`rate()` and `increase()`, which handle that. For the durable history, read the
database: `GET /api/fleet/summary` does.

## Traces

Tracing is on when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Compose points it at
Jaeger and names each service with `OTEL_SERVICE_NAME`. Without the variable
(tests, `uvicorn` on the host) nothing is instrumented or exported.

A prediction's trace:

```
model-router        POST /router/models/{model_name}/predict
├─ model-router       GET  (readiness check)
│  └─ model-serving-*   GET /ready
├─ model-router       POST (forward)
│  └─ model-serving-*   POST /predict
│     └─ model-serving-*   model.inference
└─ model-router       POST (metric push, finishes after the response)
   └─ control-plane     POST /api/deployments/{deployment_id}/metrics
```

The router span carries `modelops.model`, `modelops.version`,
`modelops.deployment_id` and `modelops.config_revision`. In Jaeger, search by tag:
`modelops.version=v2-good`. A benchmark's injected fault is tagged on the serving
span (`modelops.fault.latency_ms`, `modelops.fault.error_rate`), so a deliberately
slow request doesn't look like a model problem.

A worker sweep is a trace too: `worker.sweep` → reconcile → evaluate →
promote/rollback → the control plane's `PUT /router/config` to the router.

**Sampling.** The router and the control plane keep 1 in 10 of the traces they
start (`OTEL_TRACES_SAMPLER=parentbased_traceidratio`, `..._ARG=0.1` in compose).
Anything downstream follows its parent's decision, so a sampled trace is always
complete. Worker sweeps are few and are all kept. Set the ratio to `1.0` to see
every request.

Jaeger keeps at most 20,000 traces in memory (`MEMORY_MAX_TRACES`), so a restart
clears them. Without the cap, a benchmark grew it past 4 GB.

## Alerts

Defined in `prometheus/rules.yml`, shown in the dashboards' "Firing alerts" panel
and at http://localhost:9090/alerts. There's no Alertmanager, so nothing is sent
anywhere.

| Alert | Fires when |
|---|---|
| `ModelOpsServiceDown` | a scrape target stops answering for 30s |
| `ModelOpsWorkerStalled` | no completed sweep for 60s |
| `ModelOpsVersionErrorRateHigh` | a version fails > 5% of requests for 1m (with real traffic) |
| `ModelOpsVersionLatencyHigh` | a version's p95 is > 500 ms for 1m |
| `ModelOpsReconcileFailing` | the reconciler couldn't fix the router in the last 5m |

## Changing a dashboard

The JSON in `grafana/dashboards/` is generated:

```bash
python3 observability/grafana/build_dashboards.py
```

Edit `build_dashboards.py` rather than the JSON, or the next regeneration
overwrites your change. Grafana reloads the files within about 10 seconds, with
no restart. A UI edit can't be saved over a provisioned dashboard. Prototype in
the UI, then port the change to the script.

`backend/tests/test_observability.py` fails if a dashboard queries a metric no
service exports. That's what catches a rename that would otherwise leave a panel
silently empty.
