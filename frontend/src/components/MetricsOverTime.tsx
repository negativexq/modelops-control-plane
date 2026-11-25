"use client";

import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useMetricsTimeseries } from "@/lib/queries";
import { formatMs, parseApiDate } from "@/lib/format";
import type { DeploymentOut, MetricsTimeseries } from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * p95 latency and error rate over the life of the rollout, stable vs canary,
 * with the moments that mattered drawn on top: each traffic step and the final
 * decision. The dashed lines are this rollout's own policy limits, so a
 * rollback reads as "the canary line crossed the limit here".
 */

interface Marker {
  t: number;
  label: string;
  tone: string;
}

const ADVANCE_RE = /to (\d+(?:\.\d+)?)%/;
const DECISION_RE = /-> (PROMOTED|ROLLED_BACK|INCONCLUSIVE|FAILED)\b/;
const DECISION_TONE: Record<string, string> = {
  PROMOTED: "var(--success)",
  ROLLED_BACK: "var(--danger)",
  INCONCLUSIVE: "var(--warning)",
  FAILED: "var(--danger)",
};

function markersOf(deployment: DeploymentOut): Marker[] {
  const markers: Marker[] = [];
  for (const e of deployment.events) {
    const t = parseApiDate(e.created_at).getTime();
    if (e.event_type === "traffic_advanced") {
      const to = ADVANCE_RE.exec(e.message)?.[1];
      markers.push({ t, label: to ? `${to}%` : "advance", tone: "var(--canary)" });
    } else if (e.event_type === "status_changed") {
      const outcome = DECISION_RE.exec(e.message)?.[1];
      if (outcome) markers.push({ t, label: outcome.replace("_", " ").toLowerCase(), tone: DECISION_TONE[outcome] });
    }
  }
  return markers;
}

interface Row {
  t: number;
  stableP95: number | null;
  canaryP95: number | null;
  stableErr: number | null;
  canaryErr: number | null;
  latencyLimit: number | null;
  stableCount: number;
  canaryCount: number;
}

function rowsOf(data: MetricsTimeseries, latencyIncreasePct: number | null): Row[] {
  const stable = data.series.find((s) => s.role === "stable");
  const canary = data.series.find((s) => s.role === "canary");
  const length = Math.max(stable?.buckets.length ?? 0, canary?.buckets.length ?? 0);
  return Array.from({ length }, (_, i) => {
    const s = stable?.buckets[i];
    const c = canary?.buckets[i];
    const t = parseApiDate((s ?? c)!.t).getTime() + (data.bucket_seconds * 1000) / 2;
    const stableP95 = s?.p95_latency_ms ?? null;
    return {
      t,
      stableP95,
      canaryP95: c?.p95_latency_ms ?? null,
      stableErr: s?.error_rate != null ? s.error_rate * 100 : null,
      canaryErr: c?.error_rate != null ? c.error_rate * 100 : null,
      latencyLimit: stableP95 != null && latencyIncreasePct != null ? stableP95 * (1 + latencyIncreasePct / 100) : null,
      stableCount: s?.count ?? 0,
      canaryCount: c?.count ?? 0,
    };
  });
}

const clock = (t: number) =>
  new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function ChartTooltip({
  active,
  payload,
  label,
  format,
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number | null; color: string; payload: Row }>;
  label?: number;
  format: (v: number) => string;
}) {
  if (!active || !payload?.length || label == null) return null;
  const row = payload[0].payload;
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="mb-1 font-mono text-muted-foreground tabular">{clock(label)}</p>
      {payload.map((entry) =>
        entry.value == null ? null : (
          <p key={entry.name} className="flex items-center gap-2">
            <span className="size-2 rounded-full" style={{ background: entry.color }} />
            <span className="text-muted-foreground">{entry.name}</span>
            <span className="ml-auto pl-3 font-mono tabular">{format(entry.value)}</span>
          </p>
        ),
      )}
      <p className="mt-1 font-mono text-[10px] text-muted-foreground tabular">
        n = {row.stableCount} stable · {row.canaryCount} canary
      </p>
    </div>
  );
}

function Panel({
  title,
  rows,
  markers,
  domain,
  lines,
  limit,
  format,
  unit,
}: {
  title: string;
  rows: Row[];
  markers: Marker[];
  domain: [number, number];
  lines: Array<{ key: keyof Row; name: string; color: string; dashed?: boolean }>;
  limit?: { y: number; label: string };
  format: (v: number) => string;
  unit: string;
}) {
  return (
    <div className="rounded-lg border bg-muted/30 p-4">
      <h3 className="mb-3 text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h3>
      <div className="h-52 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 14, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={domain}
              tickFormatter={clock}
              tickLine={false}
              axisLine={false}
              fontSize={10}
              minTickGap={40}
              stroke="var(--muted-foreground)"
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              fontSize={10}
              width={44}
              unit={unit}
              // Keep the policy limit on screen even when every value sits far below it.
              domain={limit ? [0, (dataMax: number) => Math.max(dataMax, limit.y * 1.25)] : [0, "auto"]}
              stroke="var(--muted-foreground)"
            />
            <Tooltip content={<ChartTooltip format={format} />} />
            {markers.map((m) => (
              <ReferenceLine
                key={`${m.t}-${m.label}`}
                x={m.t}
                stroke={m.tone}
                strokeDasharray="2 3"
                label={{
                  value: m.label,
                  // Labels near the right edge would be clipped; flip them inward.
                  position: m.t > domain[0] + (domain[1] - domain[0]) * 0.7 ? "insideTopRight" : "insideTopLeft",
                  fontSize: 10,
                  fill: m.tone,
                }}
              />
            ))}
            {limit ? (
              <ReferenceLine
                y={limit.y}
                stroke="var(--danger)"
                strokeDasharray="4 4"
                strokeOpacity={0.7}
                label={{ value: limit.label, position: "insideTopRight", fontSize: 10, fill: "var(--danger)" }}
              />
            ) : null}
            {lines.map((line) => (
              <Line
                key={line.key}
                dataKey={line.key}
                name={line.name}
                stroke={line.color}
                strokeWidth={line.dashed ? 1 : 1.75}
                strokeDasharray={line.dashed ? "4 4" : undefined}
                dot={false}
                isAnimationActive={false}
                connectNulls={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function MetricsOverTime({ deployment, live }: { deployment: DeploymentOut; live: boolean }) {
  const query = useMetricsTimeseries(deployment.id, live);
  const policy = deployment.policy_config;
  const latencyPct = policy?.latency.p95_max_increase_percent ?? null;
  const maxErrorPct = policy?.reliability.max_error_rate_percent ?? null;

  const rows = useMemo(() => (query.data ? rowsOf(query.data, latencyPct) : []), [query.data, latencyPct]);
  const markers = useMemo(() => markersOf(deployment), [deployment]);

  if (query.isPending) {
    return (
      <div className="grid gap-4 md:grid-cols-2">
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    );
  }
  if (query.isError) return <p className="text-sm text-danger">{query.error.message}</p>;

  const data = query.data;
  const total = data.series.reduce((sum, s) => sum + s.buckets.reduce((n, b) => n + b.count, 0), 0);
  const domain: [number, number] = [parseApiDate(data.start).getTime(), parseApiDate(data.end).getTime()];
  const stable = data.series.find((s) => s.role === "stable")?.version ?? "stable";
  const canary = data.series.find((s) => s.role === "canary")?.version ?? "canary";

  return (
    <section className="space-y-3" data-testid="metrics-over-time">
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Over time</h2>
          <p className="text-xs text-muted-foreground">
            Whole rollout, {data.bucket_seconds}s buckets. Dashed lines are this rollout&apos;s policy limits.
          </p>
        </div>
        <span className="font-mono text-xs text-muted-foreground tabular">{total} requests</span>
      </div>
      {total === 0 ? (
        <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
          No traffic recorded for this rollout yet.
        </p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          <Panel
            title="p95 latency"
            rows={rows}
            markers={markers}
            domain={domain}
            unit="ms"
            format={(v) => formatMs(v)}
            lines={[
              { key: "stableP95", name: stable, color: "var(--stable)" },
              { key: "canaryP95", name: canary, color: "var(--canary)" },
              ...(latencyPct != null
                ? [{ key: "latencyLimit" as const, name: `limit (+${latencyPct}%)`, color: "var(--danger)", dashed: true }]
                : []),
            ]}
          />
          <Panel
            title="Error rate"
            rows={rows}
            markers={markers}
            domain={domain}
            unit="%"
            format={(v) => `${v.toFixed(1)}%`}
            limit={maxErrorPct != null ? { y: maxErrorPct, label: `max ${maxErrorPct}%` } : undefined}
            lines={[
              { key: "stableErr", name: stable, color: "var(--stable)" },
              { key: "canaryErr", name: canary, color: "var(--canary)" },
            ]}
          />
        </div>
      )}
    </section>
  );
}
