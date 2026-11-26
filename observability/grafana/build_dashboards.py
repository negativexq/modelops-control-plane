"""Generates the Grafana dashboards in ./dashboards from Python, so queries,
units, thresholds and colours stay consistent across ~40 panels instead of
drifting in hand-edited JSON.

    python3 observability/grafana/build_dashboards.py

Standard library only. The JSON output is committed; Grafana loads it through
provisioning (provisioning/dashboards/dashboards.yml). The backend test suite
checks every metric named here is one the services actually export
(backend/tests/test_observability.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

OUT = Path(__file__).parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}

JAEGER = "http://localhost:16686"
APP = "http://localhost:3000"

# Semantic colours, matching the ModelOps dashboard's own status tones.
GREEN, RED, ORANGE, BLUE, PURPLE, GREY = (
    "#3fb950",
    "#f85149",
    "#d29922",
    "#58a6ff",
    "#bc8cff",
    "#8b949e",
)
OUTCOME_COLORS = {"PROMOTED": GREEN, "ROLLED_BACK": RED, "INCONCLUSIVE": ORANGE, "FAILED": GREY}
VERDICT_COLORS = {"PASS": GREEN, "FAIL": RED, "INCONCLUSIVE": ORANGE}

RI = "$__rate_interval"
MODEL = 'model=~"$model"'


# --- panel helpers ----------------------------------------------------------------


class Layout:
    """Places panels left to right, wrapping at 24 columns."""

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.row_h = 0
        self.next_id = 1
        self.panels: list[dict[str, Any]] = []

    def add(self, panel: dict[str, Any], w: int, h: int) -> None:
        if self.x + w > 24:
            self.newline()
        panel["id"] = self.next_id
        self.next_id += 1
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def newline(self) -> None:
        if self.x:
            self.y += self.row_h
        self.x = 0
        self.row_h = 0

    def row(self, title: str, repeat: str | None = None) -> None:
        """`repeat`: a multi-value variable - Grafana draws this row and its panels
        once per selected value, with the variable bound to that one value."""
        self.newline()
        panel: dict[str, Any] = {"type": "row", "title": title, "collapsed": False, "panels": []}
        if repeat:
            panel["repeat"] = repeat
        self.add(panel, 24, 1)
        self.newline()


def target(expr: str, legend: str = "", ref: str = "A", **extra: Any) -> dict[str, Any]:
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend or "__auto",
        "refId": ref,
        "range": True,
        **extra,
    }


def instant(expr: str, legend: str = "", ref: str = "A") -> dict[str, Any]:
    t = target(expr, legend, ref)
    t.update(range=False, instant=True, format="table")
    return t


def thresholds(*steps: tuple[float | None, str]) -> dict[str, Any]:
    return {
        "mode": "absolute",
        "steps": [{"value": v, "color": c} for v, c in steps],
    }


def color_overrides(colors: dict[str, str], by: str = "byName") -> list[dict[str, Any]]:
    return [
        {
            "matcher": {"id": by, "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for name, color in colors.items()
    ]


def stat(
    title: str,
    targets: list[dict[str, Any]],
    unit: str = "short",
    steps: dict[str, Any] | None = None,
    description: str = "",
    decimals: int | None = None,
    color_mode: str = "background",
    graph: bool = True,
    text_mode: str = "auto",
    overrides: list[dict[str, Any]] | None = None,
    mappings: list[dict[str, Any]] | None = None,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "unit": unit,
        "color": {"mode": "thresholds"},
        "thresholds": steps or thresholds((None, BLUE)),
        "mappings": mappings or [],
        "links": links or [],
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": color_mode,
            "graphMode": "area" if graph else "none",
            "textMode": text_mode,
            "justifyMode": "center",
            "orientation": "auto",
            "wideLayout": True,
            "showPercentChange": False,
        },
    }


def timeseries(
    title: str,
    targets: list[dict[str, Any]],
    unit: str = "short",
    description: str = "",
    stack: bool = False,
    fill: int = 12,
    bars: bool = False,
    step: bool = False,
    max_: float | None = None,
    min_: float | None = 0,
    overrides: list[dict[str, Any]] | None = None,
    threshold_line: float | None = None,
    legend_calcs: tuple[str, ...] = ("mean", "max", "lastNotNull"),
    links: list[dict[str, Any]] | None = None,
    decimals: int | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    """`interval`: minimum query step. Count panels (increase over $__interval)
    need it - on a short range $__interval drops below the 5s scrape interval and
    increase() over a window with one sample returns nothing."""
    custom: dict[str, Any] = {
        "drawStyle": "bars" if bars else "line",
        "lineInterpolation": "stepAfter" if step else "smooth",
        "lineWidth": 2 if not bars else 1,
        "fillOpacity": 80 if bars else fill,
        "gradientMode": "opacity" if not bars else "none",
        "showPoints": "never",
        "spanNulls": True,
        "axisSoftMin": min_,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "barAlignment": 0,
        "axisBorderShow": False,
        "thresholdsStyle": {"mode": "dashed" if threshold_line is not None else "off"},
    }
    defaults: dict[str, Any] = {
        "unit": unit,
        "color": {"mode": "palette-classic"},
        "custom": custom,
        "links": links or [],
    }
    if max_ is not None:
        defaults["max"] = max_
    if min_ is not None:
        defaults["min"] = min_
    if decimals is not None:
        defaults["decimals"] = decimals
    if threshold_line is not None:
        defaults["thresholds"] = thresholds((None, "transparent"), (threshold_line, RED))
    extra = {"interval": interval} if interval else {}
    return {
        **extra,
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {
                "showLegend": True,
                "displayMode": "table" if legend_calcs else "list",
                "placement": "bottom",
                "calcs": list(legend_calcs),
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def heatmap(title: str, expr: str, description: str = "") -> dict[str, Any]:
    return {
        "type": "heatmap",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": [target(expr, "{{le}}", format="heatmap")],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {
            "calculate": False,
            "cellGap": 1,
            "color": {
                "mode": "scheme",
                "scheme": "Oranges",
                "steps": 64,
                "exponent": 0.5,
                "fill": ORANGE,
                "reverse": False,
            },
            "yAxis": {"unit": "s", "axisPlacement": "left", "reverse": False},
            "rowsFrame": {"layout": "auto"},
            "cellValues": {"unit": "short"},
            "tooltip": {"mode": "single", "yHistogram": True, "showColorScale": True},
            "legend": {"show": True},
            "exemplars": {"color": "rgba(255,0,255,0.7)"},
            "filterValues": {"le": 1e-9},
        },
    }


def table(
    title: str,
    targets: list[dict[str, Any]],
    description: str = "",
    transformations: list[dict[str, Any]] | None = None,
    overrides: list[dict[str, Any]] | None = None,
    no_value: str = "",
    sort_by: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "table",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": targets,
        "transformations": transformations or [],
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": False},
                "noValue": no_value,
                "color": {"mode": "thresholds"},
                "thresholds": thresholds((None, GREEN)),
            },
            "overrides": overrides or [],
        },
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": sort_by or []},
    }


def text(title: str, content: str) -> dict[str, Any]:
    return {
        "type": "text",
        "title": title,
        "options": {"mode": "markdown", "content": content},
    }


def jaeger_link(service: str, title: str, tags: dict[str, str] | None = None) -> dict[str, Any]:
    url = f"{JAEGER}/search?service={service}&lookback=1h&limit=20"
    if tags:
        # Grafana substitutes ${...} before following the link; keep them unescaped.
        url += "&tags=" + quote(json.dumps(tags), safe="${}._")
    return {"title": title, "url": url, "targetBlank": True}


def override(name: str, *props: tuple[str, Any], by: str = "byName") -> dict[str, Any]:
    return {
        "matcher": {"id": by, "options": name},
        "properties": [{"id": k, "value": v} for k, v in props],
    }


def dashboard(
    uid: str,
    title: str,
    description: str,
    layout: Layout,
    variables: list[dict[str, Any]],
    links: list[dict[str, Any]],
    tags: list[str],
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": tags,
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,  # shared crosshair: hover one panel, see the same instant everywhere
        "refresh": "10s",
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
        "fiscalYearStartMonth": 0,
        "liveNow": False,
        "schemaVersion": 39,
        "version": 1,
        "links": links,
        "templating": {"list": variables},
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                },
                {
                    # A marker on every time series wherever a rollout was decided.
                    "datasource": DS,
                    "enable": True,
                    "iconColor": PURPLE,
                    "name": "Rollout decisions",
                    # Window == step, so one decision is one marker, not a two-step region.
                    "expr": "sum by (outcome, trigger) (increase(modelops_decisions_total[15s])) > 0.5",
                    "step": "15s",
                    "titleFormat": "{{outcome}}",
                    "textFormat": "{{trigger}} decision",
                    "tagKeys": "outcome,trigger",
                    "useValueForTime": False,
                },
            ]
        },
        "panels": layout.panels,
    }


def query_var(
    name: str, label: str, query: str, multi: bool = False, include_all: bool = True
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": DS,
        "query": {"query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
        "definition": query,
        "refresh": 2,
        "sort": 1,
        "multi": multi,
        "includeAll": include_all,
        "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}
        if include_all
        else {},
        "options": [],
        "hide": 0,
    }


NAV_LINKS = [
    {
        "title": "ModelOps dashboards",
        "type": "dashboards",
        "tags": ["modelops"],
        "asDropdown": False,
        "includeVars": True,
        "keepTime": True,
    },
    {"title": "Traces (Jaeger)", "type": "link", "url": JAEGER, "targetBlank": True,
     "icon": "external link"},
    {"title": "Control plane UI", "type": "link", "url": APP, "targetBlank": True,
     "icon": "external link"},
]


# --- shared expressions -----------------------------------------------------------


def router_rate(sel: str = MODEL, by: str = "") -> str:
    grp = f" by ({by})" if by else ""
    return f"sum{grp} (rate(modelops_router_requests_total{{{sel}}}[{RI}]))"


def router_errors(sel: str = MODEL, by: str = "") -> str:
    grp = f" by ({by})" if by else ""
    return f'sum{grp} (rate(modelops_router_requests_total{{{sel}, status=~"5.."}}[{RI}]))'


def error_ratio(sel: str = MODEL, by: str = "") -> str:
    # `or ... * 0` keeps a 0% line for versions that have traffic but no errors,
    # instead of no series at all.
    return f"({router_errors(sel, by)} or {router_rate(sel, by)} * 0) / {router_rate(sel, by)}"


def router_quantile(q: float, sel: str = MODEL, by: str = "") -> str:
    grp = ", ".join(["le", *([by] if by else [])])
    return (
        f"histogram_quantile({q}, sum by ({grp}) "
        f"(rate(modelops_router_upstream_latency_seconds_bucket{{{sel}}}[{RI}])))"
    )


def http_quantile(q: float, sel: str, by: str = "") -> str:
    grp = ", ".join(["le", *([by] if by else [])])
    return (
        f"histogram_quantile({q}, sum by ({grp}) "
        f"(rate(modelops_http_request_duration_seconds_bucket{{{sel}}}[{RI}])))"
    )


ERROR_STEPS = thresholds((None, GREEN), (0.01, ORANGE), (0.05, RED))
LATENCY_STEPS = thresholds((None, GREEN), (0.25, ORANGE), (0.5, RED))


# --- dashboard 1: rollouts --------------------------------------------------------


def rollouts() -> dict[str, Any]:
    L = Layout()

    # Headline numbers ---------------------------------------------------------
    L.add(
        stat(
            "Predictions / s",
            [target(router_rate())],
            unit="reqps",
            decimals=1,
            steps=thresholds((None, BLUE)),
            description="Requests the router sent to a model version, all versions of the "
            "selected model(s).",
        ),
        4,
        5,
    )
    L.add(
        stat(
            "Error rate",
            [target(f"({router_errors()} or vector(0)) / {router_rate()}")],
            unit="percentunit",
            decimals=2,
            steps=ERROR_STEPS,
            description="5xx share of routed predictions. The default rollback gate is 5% "
            "above the stable version.",
        ),
        4,
        5,
    )
    L.add(
        stat(
            "p95 latency",
            [target(router_quantile(0.95))],
            unit="s",
            decimals=0,
            steps=LATENCY_STEPS,
            description="Router to model service round trip, 95th percentile.",
        ),
        4,
        5,
    )
    L.add(
        stat(
            "Live rollouts",
            [target("max(modelops_worker_active_rollouts)")],
            steps=thresholds((None, GREY), (1, PURPLE)),
            graph=False,
            description="Active, automation-enabled rollouts the worker saw on its last "
            "sweep.",
            links=[{"title": "Open the control plane", "url": APP, "targetBlank": True}],
        ),
        3,
        5,
    )
    L.add(
        stat(
            "Decisions in range",
            [
                target(
                    f'round(sum(increase(modelops_decisions_total{{outcome="{outcome}"}}'
                    f"[$__range])) or vector(0))",
                    label,
                    ref,
                )
                for ref, (outcome, label) in zip(
                    "ABC",
                    (
                        ("PROMOTED", "Promoted"),
                        ("ROLLED_BACK", "Rolled back"),
                        ("INCONCLUSIVE", "Inconclusive"),
                    ),
                )
            ],
            decimals=0,
            graph=False,
            color_mode="value",
            text_mode="value_and_name",
            overrides=color_overrides(
                {"Promoted": GREEN, "Rolled back": RED, "Inconclusive": ORANGE}
            ),
            description="Rollouts that reached a decision in the selected time range, "
            "counted by the control plane since it last started.",
        ),
        5,
        5,
    )
    L.add(
        stat(
            "Worker heartbeat",
            [target("time() - max(modelops_worker_last_sweep_timestamp_seconds)")],
            unit="s",
            decimals=0,
            graph=False,
            steps=thresholds((None, GREEN), (30, ORANGE), (60, RED)),
            description="Seconds since the control loop last finished a sweep. Red means "
            "rollouts aren't being evaluated.",
            mappings=[
                {
                    "type": "special",
                    "options": {"match": "null", "result": {"text": "no data", "color": RED}},
                }
            ],
        ),
        4,
        5,
    )

    L.add(
        table(
            "Firing alerts",
            [instant('ALERTS{alertstate="firing"}')],
            description="Prometheus alert rules (observability/prometheus/rules.yml) "
            "currently firing.",
            no_value="All clear - no alerts firing",
            transformations=[
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Time": True,
                            "Value": True,
                            "__name__": True,
                            "alertstate": True,
                        },
                        "indexByName": {"alertname": 0, "severity": 1},
                    },
                }
            ],
            overrides=[
                override(
                    "severity",
                    (
                        "custom.cellOptions",
                        {"type": "color-background", "mode": "basic"},
                    ),
                    (
                        "mappings",
                        [
                            {
                                "type": "value",
                                "options": {
                                    "critical": {"color": RED, "index": 0},
                                    "warning": {"color": ORANGE, "index": 1},
                                },
                            }
                        ],
                    ),
                    ("custom.width", 110),
                )
            ],
        ),
        24,
        3,
    )

    # Control loop -------------------------------------------------------------
    L.row("Control loop")
    L.add(
        timeseries(
            "Decisions",
            [
                target(
                    "round(sum by (outcome, trigger) (increase(modelops_decisions_total[$__interval]))) > 0",
                    "{{outcome}} ({{trigger}})",
                )
            ],
            bars=True,
            stack=True,
            legend_calcs=("sum",),
            interval="1m",
            decimals=0,
            description="Promotions, rollbacks and inconclusive freezes, split by "
            "whether the worker or an operator decided.",
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": f"^{name} .*"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}
                    ],
                }
                for name, color in OUTCOME_COLORS.items()
            ],
        ),
        8,
        8,
    )
    L.add(
        timeseries(
            "Policy verdicts",
            [
                target(
                    "round(sum by (result) (increase(modelops_policy_evaluations_total[$__interval]))) > 0",
                    "{{result}}",
                )
            ],
            bars=True,
            stack=True,
            legend_calcs=("sum",),
            interval="1m",
            decimals=0,
            description="Overall verdict of each policy evaluation the worker requested.",
            overrides=color_overrides(VERDICT_COLORS),
        ),
        8,
        8,
    )
    L.add(
        # A table rather than a bar gauge: Grafana's bar gauge drops the name
        # when there's a single bar, and here one failing gate is the usual case.
        table(
            "Failing checks by policy",
            [
                instant(
                    'round(sum by (policy) (increase(modelops_policy_checks_total{result="FAIL"}'
                    "[$__range]))) > 0"
                )
            ],
            description="Which gates fail most often in the selected range.",
            no_value="No failed checks in this range",
            transformations=[
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {"policy": "Gate", "Value": "Failures"},
                    },
                }
            ],
            sort_by=[{"displayName": "Failures", "desc": True}],
            overrides=[
                override(
                    "Failures",
                    (
                        "custom.cellOptions",
                        {"type": "gauge", "mode": "gradient", "valueDisplayMode": "text"},
                    ),
                    ("thresholds", thresholds((None, ORANGE), (5, RED))),
                    ("min", 0),
                    ("decimals", 0),
                ),
                override("Gate", ("custom.width", 190)),
            ],
        ),
        8,
        8,
    )
    L.add(
        timeseries(
            "Worker actions",
            [
                target(
                    "round(sum by (action) (increase(modelops_worker_actions_total[$__interval]))) > 0",
                    "{{action}}",
                )
            ],
            bars=True,
            stack=True,
            legend_calcs=("sum",),
            interval="1m",
            decimals=0,
            description="What the worker did after each evaluation.",
            overrides=color_overrides(
                {
                    "advance_traffic": BLUE,
                    "promote": GREEN,
                    "rollback": RED,
                    "record_inconclusive": ORANGE,
                }
            ),
        ),
        8,
        8,
    )
    L.add(
        timeseries(
            "Sweep duration",
            [
                target(
                    "histogram_quantile(0.95, sum by (le) "
                    f"(rate(modelops_worker_sweep_duration_seconds_bucket[{RI}])))",
                    "p95",
                ),
                target(
                    "histogram_quantile(0.5, sum by (le) "
                    f"(rate(modelops_worker_sweep_duration_seconds_bucket[{RI}])))",
                    "p50",
                    "B",
                ),
                target("max(modelops_worker_active_rollouts)", "active rollouts", "C"),
            ],
            unit="s",
            fill=6,
            description="Time for one pass over every active rollout, with the number of "
            "rollouts it had to look at (right axis).",
            overrides=[
                override(
                    "active rollouts",
                    ("unit", "short"),
                    ("custom.axisPlacement", "right"),
                    ("custom.drawStyle", "bars"),
                    ("custom.fillOpacity", 25),
                    ("color", {"mode": "fixed", "fixedColor": PURPLE}),
                    ("decimals", 0),
                )
            ],
        ),
        8,
        8,
    )
    L.add(
        timeseries(
            "Router reconcile",
            [
                target(
                    "sum by (result) (increase(modelops_reconcile_runs_total[$__interval]))",
                    "{{result}}",
                ),
                target(
                    "sum by (model) (increase(modelops_router_push_failures_total[$__interval]))",
                    "push failed after decision ({{model}})",
                    "B",
                ),
            ],
            bars=True,
            stack=True,
            legend_calcs=("sum",),
            interval="1m",
            decimals=0,
            description="Desired (DB) vs observed (router) comparisons per model. "
            "'corrected' means the reconciler repaired drift; red means it couldn't.",
            overrides=color_overrides(
                {
                    "in_sync": GREY,
                    "corrected": BLUE,
                    "router_unreachable": RED,
                    "push_failed": RED,
                }
            )
            + [
                {
                    "matcher": {"id": "byRegexp", "options": "^push failed.*"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": ORANGE}}
                    ],
                }
            ],
        ),
        8,
        8,
    )

    # Canary vs stable ---------------------------------------------------------
    L.row("Canary vs stable · $model", repeat="model")
    version_link = [
        jaeger_link(
            "model-router",
            "Traces for ${__field.labels.version}",
            {"modelops.version": "${__field.labels.version}"},
        )
    ]
    L.add(
        timeseries(
            "Traffic split",
            [target("max by (version) (modelops_router_traffic_weight{" + MODEL + "})",
                    "{{version}}")],
            unit="percentunit",
            stack=True,
            fill=55,
            step=True,
            max_=1,
            # "last", not lastNotNull: a version that left the split shows empty.
            legend_calcs=("last",),
            description="Share of traffic the router sends to each version, as applied "
            "(observed state). Each step is a rollout stage, a promote or a rollback.",
        ),
        12,
        9,
    )
    L.add(
        timeseries(
            "Requests by version",
            [target(router_rate(by="version"), "{{version}}")],
            unit="reqps",
            description="Routed predictions per second, by the version that served them.",
            links=version_link,
        ),
        12,
        9,
    )
    L.add(
        timeseries(
            "p95 latency by version",
            [
                target(router_quantile(0.95, by="version"), "p95 {{version}}"),
                target(router_quantile(0.5, by="version"), "p50 {{version}}", "B"),
            ],
            unit="s",
            fill=0,
            threshold_line=0.5,
            description="Router-measured round trip to each version. Solid: p95, dashed: "
            "p50. Red line: 500 ms, where the latency alert fires. Click a series for its "
            "traces.",
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": "^p50 .*"},
                    "properties": [
                        {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}},
                        {"id": "custom.lineWidth", "value": 1},
                    ],
                }
            ],
            links=version_link,
        ),
        12,
        9,
    )
    L.add(
        timeseries(
            "Error rate by version",
            [target(error_ratio(by="version"), "{{version}}")],
            unit="percentunit",
            fill=8,
            threshold_line=0.05,
            decimals=1,
            description="5xx share per version. Red line: 5%, where the error alert fires.",
            links=version_link,
        ),
        12,
        9,
    )
    L.add(
        heatmap(
            "Latency distribution",
            f"sum by (le) (increase(modelops_router_upstream_latency_seconds_bucket{{{MODEL}}}"
            f"[{RI}]))",
            description="Every routed request, bucketed by latency. A canary with a slow "
            "tail shows up as a second band above the main one.",
        ),
        12,
        9,
    )
    L.add(
        timeseries(
            "Responses by status",
            [target(router_rate(by="version, status"), "{{version}} · {{status}}")],
            unit="reqps",
            stack=True,
            fill=35,
            legend_calcs=("mean", "max"),
            description="Routed predictions by version and HTTP status.",
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": r".* · 5\d\d"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": RED}}
                    ],
                },
                {
                    "matcher": {"id": "byRegexp", "options": r".* · 4\d\d"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": ORANGE}}
                    ],
                },
            ],
        ),
        12,
        9,
    )

    return dashboard(
        "modelops-overview",
        "ModelOps · Rollouts",
        "Canary rollouts as the router and the control loop see them.",
        L,
        [
            query_var(
                "model",
                "Model",
                "label_values(modelops_router_traffic_weight, model)",
                multi=True,
            )
        ],
        NAV_LINKS,
        ["modelops"],
    )


# --- dashboard 2: services --------------------------------------------------------


def services() -> dict[str, Any]:
    L = Layout()
    svc = 'service=~"$service"'

    L.add(
        stat(
            "Targets up",
            [target("up", "{{job}} {{route_version}}")],
            graph=False,
            text_mode="value_and_name",
            color_mode="background",
            steps=thresholds((None, RED), (1, GREEN)),
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "DOWN", "color": RED},
                        "1": {"text": "UP", "color": GREEN},
                    },
                }
            ],
            description="Every scrape target. Red: Prometheus can't reach its /metrics.",
        ),
        24,
        4,
    )
    L.add(
        stat(
            "Requests / s",
            [target(f"sum(rate(modelops_http_requests_total{{{svc}}}[{RI}]))")],
            unit="reqps",
            decimals=1,
        ),
        6,
        5,
    )
    L.add(
        stat(
            "5xx rate",
            [
                target(
                    f'(sum(rate(modelops_http_requests_total{{{svc}, status=~"5.."}}[{RI}])) '
                    "or vector(0)) / "
                    f"sum(rate(modelops_http_requests_total{{{svc}}}[{RI}]))"
                )
            ],
            unit="percentunit",
            decimals=2,
            steps=ERROR_STEPS,
        ),
        6,
        5,
    )
    L.add(
        stat(
            "p95 latency",
            [target(http_quantile(0.95, svc))],
            unit="s",
            steps=LATENCY_STEPS,
            description="Time to first byte across the selected services.",
        ),
        6,
        5,
    )
    L.add(
        stat(
            "Model inference p95",
            [
                target(
                    "histogram_quantile(0.95, sum by (le) "
                    f"(rate(modelops_serving_inference_seconds_bucket[{RI}])))"
                )
            ],
            unit="s",
            steps=thresholds((None, GREEN), (0.05, ORANGE), (0.2, RED)),
            description="Time inside the model (predict + predict_proba), excluding HTTP "
            "and injected faults.",
        ),
        6,
        5,
    )

    L.row("Traffic")
    L.add(
        timeseries(
            "Requests by service",
            [
                target(
                    f"sum by (service) (rate(modelops_http_requests_total{{{svc}}}[{RI}]))",
                    "{{service}}",
                )
            ],
            unit="reqps",
            overrides=color_overrides(
                {"backend": PURPLE, "router": BLUE, "serving": GREEN}
            ),
        ),
        12,
        8,
    )
    L.add(
        timeseries(
            "p95 latency by service",
            [target(http_quantile(0.95, svc, "service"), "{{service}}")],
            unit="s",
            fill=0,
            overrides=color_overrides(
                {"backend": PURPLE, "router": BLUE, "serving": GREEN}
            ),
        ),
        12,
        8,
    )

    L.row("Routes · $service")
    route_sel = f'{svc}, route!="unmatched"'
    L.add(
        table(
            "Slowest and busiest routes",
            [
                instant(
                    f"sum by (service, method, route) "
                    f"(rate(modelops_http_requests_total{{{route_sel}}}[5m]))",
                    ref="A",
                ),
                instant(
                    f'(sum by (service, method, route) (rate(modelops_http_requests_total'
                    f'{{{route_sel}, status=~"5.."}}[5m])) or sum by (service, method, route) '
                    f"(rate(modelops_http_requests_total{{{route_sel}}}[5m])) * 0) / "
                    f"sum by (service, method, route) "
                    f"(rate(modelops_http_requests_total{{{route_sel}}}[5m]))",
                    ref="B",
                ),
                instant(
                    "histogram_quantile(0.95, sum by (le, service, method, route) "
                    f"(rate(modelops_http_request_duration_seconds_bucket{{{route_sel}}}[5m])))",
                    ref="C",
                ),
                instant(
                    "histogram_quantile(0.5, sum by (le, service, method, route) "
                    f"(rate(modelops_http_request_duration_seconds_bucket{{{route_sel}}}[5m])))",
                    ref="D",
                ),
            ],
            description="Last 5 minutes, per route template. Sorted by p95.",
            transformations=[
                {"id": "merge", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {
                            "Value #A": "req/s",
                            "Value #B": "5xx",
                            "Value #C": "p95",
                            "Value #D": "p50",
                        },
                        "indexByName": {
                            "service": 0,
                            "method": 1,
                            "route": 2,
                            "Value #A": 3,
                            "Value #D": 4,
                            "Value #C": 5,
                            "Value #B": 6,
                        },
                    },
                },
            ],
            sort_by=[{"displayName": "p95", "desc": True}],
            overrides=[
                override("req/s", ("unit", "reqps"), ("decimals", 2), ("custom.width", 110)),
                override("p50", ("unit", "s")),
                override(
                    "p95",
                    ("unit", "s"),
                    ("custom.cellOptions", {"type": "gauge", "mode": "lcd", "valueDisplayMode": "text"}),
                    ("thresholds", LATENCY_STEPS),
                    ("max", 1),
                ),
                override(
                    "5xx",
                    ("unit", "percentunit"),
                    ("decimals", 1),
                    ("custom.cellOptions", {"type": "color-text"}),
                    ("thresholds", ERROR_STEPS),
                ),
                override("service", ("custom.width", 90)),
                override("method", ("custom.width", 80)),
                override("route", ("custom.width", 380)),
            ],
        ),
        24,
        10,
    )

    L.row("Model services")
    L.add(
        timeseries(
            "Predictions by container",
            [
                target(
                    'sum by (route_version) (rate(modelops_http_requests_total{service="serving",'
                    f' route="/predict"}}[{RI}]))',
                    "{{route_version}}",
                )
            ],
            unit="reqps",
            description="/predict calls each serving container answered.",
        ),
        8,
        8,
    )
    L.add(
        timeseries(
            "Inference p95 by container",
            [
                target(
                    "histogram_quantile(0.95, sum by (le, route_version) "
                    f"(rate(modelops_serving_inference_seconds_bucket[{RI}])))",
                    "{{route_version}}",
                )
            ],
            unit="s",
            fill=0,
            description="Model time only. If this is flat while router latency climbs, the "
            "slowness is in the network or an injected fault, not the model.",
        ),
        8,
        8,
    )
    L.add(
        timeseries(
            "/predict errors by container",
            [
                target(
                    'sum by (route_version, status) (rate(modelops_http_requests_total{'
                    f'service="serving", route="/predict", status=~"5.."}}[{RI}]))',
                    "{{route_version}} · {{status}}",
                )
            ],
            unit="reqps",
            bars=True,
            stack=True,
            legend_calcs=("mean", "max"),
        ),
        8,
        8,
    )

    L.row("Tracing")
    L.add(
        text(
            "Traces",
            "Every prediction is one trace: **model-router** → readiness check → "
            "**model-serving-*** `/predict` → `model.inference`, plus the router's metric "
            "push to the **control-plane**. Worker sweeps are traces too "
            "(`worker.sweep` → evaluate → promote/rollback → router config push).\n\n"
            f"[Router traces]({JAEGER}/search?service=model-router&lookback=1h) · "
            f"[Slow predictions > 250 ms]({JAEGER}/search?service=model-router&"
            "minDuration=250ms&lookback=1h) · "
            f"[Failed predictions]({JAEGER}/search?service=model-router&tags=%7B%22error%22"
            "%3A%22true%22%7D&lookback=1h) · "
            f"[Worker sweeps]({JAEGER}/search?service=worker&lookback=1h)",
        ),
        24,
        4,
    )

    return dashboard(
        "modelops-services",
        "ModelOps · Services",
        "Rate, errors and duration for every ModelOps service.",
        L,
        [
            query_var(
                "service",
                "Service",
                "label_values(modelops_http_requests_total, service)",
                multi=True,
            )
        ],
        NAV_LINKS,
        ["modelops"],
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, build in (("modelops-overview", rollouts), ("modelops-services", services)):
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(build(), indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
