"use client";

import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { getDeploymentComparison } from "@/lib/api";
import { useAsync } from "@/lib/useAsync";
import { AsyncBoundary } from "@/components/AsyncBoundary";
import { formatMs, formatNumber, formatPercent } from "@/lib/format";
import type { ComparisonOut, MetricsSummary } from "@/lib/types";

const WINDOW_OPTIONS = [
  { label: "Last 1 min", value: 60 },
  { label: "Last 5 min", value: 300 },
  { label: "Last 15 min", value: 900 },
  { label: "Last 1 hour", value: 3600 },
  { label: "Last 24 hours", value: 86400 },
];

const STABLE_COLOR = "var(--color-stable, #2563eb)";
const CANARY_COLOR = "var(--color-canary, #7c3aed)";

function latencyChartData(stable: MetricsSummary, canary: MetricsSummary) {
  return [
    { metric: "p50", stable: stable.p50_latency_ms, canary: canary.p50_latency_ms },
    { metric: "p95", stable: stable.p95_latency_ms, canary: canary.p95_latency_ms },
    { metric: "p99", stable: stable.p99_latency_ms, canary: canary.p99_latency_ms },
  ];
}

function latencyTooltipFormatter(value: unknown): string {
  return typeof value === "number" ? formatMs(value) : "N/A";
}

function percentTooltipFormatter(value: unknown): string {
  return typeof value === "number" ? `${value.toFixed(2)}%` : "N/A";
}

function errorRateChartData(stable: MetricsSummary, canary: MetricsSummary) {
  return [
    {
      metric: "error rate",
      stable: stable.error_rate != null ? stable.error_rate * 100 : null,
      canary: canary.error_rate != null ? canary.error_rate * 100 : null,
    },
  ];
}

function DeltaCell({ value, higherIsWorse = true }: { value: number | null; higherIsWorse?: boolean }) {
  if (value == null) {
    return <span className="text-zinc-400 dark:text-zinc-600">N/A</span>;
  }
  const isWorse = higherIsWorse ? value > 0 : value < 0;
  const isBetter = higherIsWorse ? value < 0 : value > 0;
  const color = isWorse
    ? "text-red-600 dark:text-red-400"
    : isBetter
      ? "text-emerald-600 dark:text-emerald-400"
      : "text-zinc-600 dark:text-zinc-400";
  const sign = value > 0 ? "+" : "";
  return <span className={color}>{sign}{value.toFixed(2)}</span>;
}

function ComparisonContent({ comparison }: { comparison: ComparisonOut }) {
  const { stable, canary, deltas } = comparison;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Latency (ms)
          </h3>
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={latencyChartData(stable, canary)}>
                <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
                <XAxis dataKey="metric" fontSize={12} />
                <YAxis fontSize={12} />
                <Tooltip formatter={latencyTooltipFormatter} />
                <Legend />
                <Bar dataKey="stable" name={`stable (${stable.version})`} fill={STABLE_COLOR} />
                <Bar dataKey="canary" name={`canary (${canary.version})`} fill={CANARY_COLOR} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Error rate (%)
          </h3>
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={errorRateChartData(stable, canary)}>
                <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
                <XAxis dataKey="metric" fontSize={12} />
                <YAxis fontSize={12} />
                <Tooltip formatter={percentTooltipFormatter} />
                <Legend />
                <Bar dataKey="stable" name={`stable (${stable.version})`} fill={STABLE_COLOR} />
                <Bar dataKey="canary" name={`canary (${canary.version})`} fill={CANARY_COLOR} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead>
            <tr className="border-b border-zinc-200 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
              <th className="py-2 pr-4">Metric</th>
              <th className="py-2 pr-4">Stable ({stable.version})</th>
              <th className="py-2 pr-4">Canary ({canary.version})</th>
              <th className="py-2 pr-4">Δ (canary − stable)</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100 dark:divide-zinc-900">
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">Samples</td>
              <td className="py-2 pr-4">{stable.sample_count}</td>
              <td className="py-2 pr-4">{canary.sample_count}</td>
              <td className="py-2 pr-4">—</td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">p50 latency</td>
              <td className="py-2 pr-4">{formatMs(stable.p50_latency_ms)}</td>
              <td className="py-2 pr-4">{formatMs(canary.p50_latency_ms)}</td>
              <td className="py-2 pr-4">—</td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">p95 latency</td>
              <td className="py-2 pr-4">{formatMs(stable.p95_latency_ms)}</td>
              <td className="py-2 pr-4">{formatMs(canary.p95_latency_ms)}</td>
              <td className="py-2 pr-4">
                <DeltaCell value={deltas.p95_latency_ms} />
              </td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">p99 latency</td>
              <td className="py-2 pr-4">{formatMs(stable.p99_latency_ms)}</td>
              <td className="py-2 pr-4">{formatMs(canary.p99_latency_ms)}</td>
              <td className="py-2 pr-4">—</td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">Error rate</td>
              <td className="py-2 pr-4">{formatPercent(stable.error_rate)}</td>
              <td className="py-2 pr-4">{formatPercent(canary.error_rate)}</td>
              <td className="py-2 pr-4">
                <DeltaCell value={deltas.error_rate != null ? deltas.error_rate * 100 : null} />
              </td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">Labeled samples</td>
              <td className="py-2 pr-4">{stable.labeled_sample_count}</td>
              <td className="py-2 pr-4">{canary.labeled_sample_count}</td>
              <td className="py-2 pr-4">—</td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">Label coverage</td>
              <td className="py-2 pr-4">{formatPercent(stable.label_coverage)}</td>
              <td className="py-2 pr-4">{formatPercent(canary.label_coverage)}</td>
              <td className="py-2 pr-4">—</td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">Positive labels</td>
              <td className="py-2 pr-4">{stable.positive_label_count}</td>
              <td className="py-2 pr-4">{canary.positive_label_count}</td>
              <td className="py-2 pr-4">—</td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">Precision</td>
              <td className="py-2 pr-4">{formatNumber(stable.precision)}</td>
              <td className="py-2 pr-4">{formatNumber(canary.precision)}</td>
              <td className="py-2 pr-4">—</td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">Recall</td>
              <td className="py-2 pr-4">{formatNumber(stable.recall)}</td>
              <td className="py-2 pr-4">{formatNumber(canary.recall)}</td>
              <td className="py-2 pr-4">
                <DeltaCell
                  value={deltas.recall}
                  higherIsWorse={false}
                />
              </td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-zinc-500 dark:text-zinc-400">False positive rate</td>
              <td className="py-2 pr-4">{formatNumber(stable.false_positive_rate)}</td>
              <td className="py-2 pr-4">{formatNumber(canary.false_positive_rate)}</td>
              <td className="py-2 pr-4">—</td>
            </tr>
          </tbody>
        </table>
        {stable.precision == null && canary.precision == null ? (
          <p className="mt-3 text-xs text-zinc-500 dark:text-zinc-400">
            Precision/recall/false-positive-rate show N/A because no ground-truth label has
            landed yet for this window. Labels arrive delayed through{" "}
            <code>POST /api/labels</code> - see the Labeled samples / Label coverage rows above.
          </p>
        ) : null}
      </div>
    </div>
  );
}

export function CanaryAnalysis({ deploymentId }: { deploymentId: string }) {
  const [windowSeconds, setWindowSeconds] = useState(300);
  const { data, error, loading, refetch } = useAsync(
    () => getDeploymentComparison(deploymentId, windowSeconds),
    [deploymentId, windowSeconds],
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Canary analysis
        </h2>
        <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
          Window
          <select
            value={windowSeconds}
            onChange={(event) => setWindowSeconds(Number(event.target.value))}
            className="rounded-md border border-zinc-300 bg-white px-2 py-1 dark:border-zinc-700 dark:bg-zinc-900"
          >
            {WINDOW_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <AsyncBoundary data={data} error={error} loading={loading} onRetry={refetch}>
        {(comparison) => <ComparisonContent comparison={comparison} />}
      </AsyncBoundary>
    </div>
  );
}
