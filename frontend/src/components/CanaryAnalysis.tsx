"use client";

import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Info } from "lucide-react";
import { useComparison } from "@/lib/queries";
import { formatMs, formatNumber, formatPercent } from "@/lib/format";
import type { ComparisonOut, MetricsSummary } from "@/lib/types";
import { cn } from "@/lib/utils";
import { QueryState } from "@/components/QueryState";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const WINDOW_OPTIONS = [
  { label: "Last 1 min", value: 60 },
  { label: "Last 5 min", value: 300 },
  { label: "Last 15 min", value: 900 },
  { label: "Last 1 hour", value: 3600 },
  { label: "Last 24 hours", value: 86400 },
];

const STABLE_COLOR = "var(--stable)";
const CANARY_COLOR = "var(--canary)";

function latencyChartData(stable: MetricsSummary, canary: MetricsSummary) {
  return [
    { metric: "p50", stable: stable.p50_latency_ms, canary: canary.p50_latency_ms },
    { metric: "p95", stable: stable.p95_latency_ms, canary: canary.p95_latency_ms },
    { metric: "p99", stable: stable.p99_latency_ms, canary: canary.p99_latency_ms },
  ];
}

function ChartTooltip({
  active,
  payload,
  label,
  format,
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number | null; color: string }>;
  label?: string;
  format: (value: number | null) => string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="mb-1 font-medium">{label}</p>
      {payload.map((entry) => (
        <p key={entry.name} className="flex items-center gap-2 text-muted-foreground">
          <span className="size-2 rounded-full" style={{ background: entry.color }} />
          {entry.name}
          <span className="ml-auto pl-3 font-mono text-foreground tabular">{format(entry.value)}</span>
        </p>
      ))}
    </div>
  );
}

function MiniBarChart({
  title,
  data,
  stableName,
  canaryName,
  format,
}: {
  title: string;
  data: Array<{ metric: string; stable: number | null; canary: number | null }>;
  stableName: string;
  canaryName: string;
  format: (value: number | null) => string;
}) {
  return (
    <div className="rounded-lg border bg-muted/30 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h3>
        <div className="flex gap-3 text-[11px] text-muted-foreground">
          <span className="inline-flex items-center gap-1.5">
            <span className="size-2 rounded-full bg-stable" />
            <span className="font-mono">{stableName}</span>
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="size-2 rounded-full bg-canary" />
            <span className="font-mono">{canaryName}</span>
          </span>
        </div>
      </div>
      <div className="h-48 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} barGap={4} barCategoryGap="28%">
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis
              dataKey="metric"
              tickLine={false}
              axisLine={false}
              fontSize={11}
              stroke="var(--muted-foreground)"
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              fontSize={11}
              width={36}
              stroke="var(--muted-foreground)"
            />
            <Tooltip
              cursor={{ fill: "var(--accent)", opacity: 0.4 }}
              content={<ChartTooltip format={format} />}
            />
            <Bar dataKey="stable" name={stableName} fill={STABLE_COLOR} radius={[3, 3, 0, 0]} />
            <Bar dataKey="canary" name={canaryName} fill={CANARY_COLOR} radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function DeltaCell({ value, higherIsWorse = true }: { value: number | null; higherIsWorse?: boolean }) {
  if (value == null) return <span className="text-muted-foreground/60">N/A</span>;
  const isWorse = higherIsWorse ? value > 0 : value < 0;
  const isBetter = higherIsWorse ? value < 0 : value > 0;
  const sign = value > 0 ? "+" : "";
  return (
    <span className={cn(isWorse && "text-danger", isBetter && "text-success")}>
      {sign}
      {value.toFixed(2)}
    </span>
  );
}

function ComparisonContent({ comparison }: { comparison: ComparisonOut }) {
  const { stable, canary, deltas } = comparison;

  const rows: Array<{ label: string; stable: string; canary: string; delta?: React.ReactNode }> = [
    { label: "Samples", stable: String(stable.sample_count), canary: String(canary.sample_count) },
    { label: "p50 latency", stable: formatMs(stable.p50_latency_ms), canary: formatMs(canary.p50_latency_ms) },
    {
      label: "p95 latency",
      stable: formatMs(stable.p95_latency_ms),
      canary: formatMs(canary.p95_latency_ms),
      delta: <DeltaCell value={deltas.p95_latency_ms} />,
    },
    { label: "p99 latency", stable: formatMs(stable.p99_latency_ms), canary: formatMs(canary.p99_latency_ms) },
    {
      label: "Error rate",
      stable: formatPercent(stable.error_rate),
      canary: formatPercent(canary.error_rate),
      delta: <DeltaCell value={deltas.error_rate != null ? deltas.error_rate * 100 : null} />,
    },
    {
      label: "Labeled samples",
      stable: String(stable.labeled_sample_count),
      canary: String(canary.labeled_sample_count),
    },
    {
      label: "Label coverage",
      stable: formatPercent(stable.label_coverage),
      canary: formatPercent(canary.label_coverage),
    },
    {
      label: "Positive labels",
      stable: String(stable.positive_label_count),
      canary: String(canary.positive_label_count),
    },
    { label: "Precision", stable: formatNumber(stable.precision), canary: formatNumber(canary.precision) },
    {
      label: "Recall",
      stable: formatNumber(stable.recall),
      canary: formatNumber(canary.recall),
      delta: <DeltaCell value={deltas.recall} higherIsWorse={false} />,
    },
    {
      label: "False positive rate",
      stable: formatNumber(stable.false_positive_rate),
      canary: formatNumber(canary.false_positive_rate),
    },
  ];

  return (
    <div className="space-y-5">
      <div className="grid gap-4 md:grid-cols-2">
        <MiniBarChart
          title="Latency (ms)"
          data={latencyChartData(stable, canary)}
          stableName={stable.version}
          canaryName={canary.version}
          format={(v) => (v == null ? "N/A" : formatMs(v))}
        />
        <MiniBarChart
          title="Error rate (%)"
          data={[
            {
              metric: "error rate",
              stable: stable.error_rate != null ? stable.error_rate * 100 : null,
              canary: canary.error_rate != null ? canary.error_rate * 100 : null,
            },
          ]}
          stableName={stable.version}
          canaryName={canary.version}
          format={(v) => (v == null ? "N/A" : `${v.toFixed(2)}%`)}
        />
      </div>

      <div className="overflow-hidden rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow className="bg-muted/40 hover:bg-muted/40">
              <TableHead>Metric</TableHead>
              <TableHead className="text-right">
                <span className="inline-flex items-center gap-1.5">
                  <span className="size-2 rounded-full bg-stable" />
                  <span className="font-mono">{stable.version}</span>
                </span>
              </TableHead>
              <TableHead className="text-right">
                <span className="inline-flex items-center gap-1.5">
                  <span className="size-2 rounded-full bg-canary" />
                  <span className="font-mono">{canary.version}</span>
                </span>
              </TableHead>
              <TableHead className="text-right">Δ canary − stable</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody className="font-mono text-xs tabular">
            {rows.map((row) => (
              <TableRow key={row.label}>
                <TableCell className="font-sans text-sm text-muted-foreground">{row.label}</TableCell>
                <TableCell className="text-right">{row.stable}</TableCell>
                <TableCell className="text-right">{row.canary}</TableCell>
                <TableCell className="text-right">{row.delta ?? <span className="text-muted-foreground/60">—</span>}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {stable.precision == null && canary.precision == null ? (
        <p className="flex items-start gap-2 text-xs text-muted-foreground">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>
            Precision, recall and FPR read N/A until a ground-truth label lands for this window.
            Labels arrive delayed through <code className="font-mono">POST /api/labels</code>.
          </span>
        </p>
      ) : null}
    </div>
  );
}

export function CanaryAnalysis({ deploymentId, live }: { deploymentId: string; live: boolean }) {
  const [windowSeconds, setWindowSeconds] = useState(300);
  const query = useComparison(deploymentId, windowSeconds, live);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Canary analysis</h2>
          <p className="text-xs text-muted-foreground">Stable vs canary over the selected window.</p>
        </div>
        <Select value={String(windowSeconds)} onValueChange={(value) => setWindowSeconds(Number(value))}>
          <SelectTrigger size="sm" className="w-36" aria-label="Analysis window">
            <SelectValue />
          </SelectTrigger>
          <SelectContent align="end">
            {WINDOW_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={String(option.value)}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <QueryState
        query={query}
        skeleton={
          <div className="grid gap-4 md:grid-cols-2">
            <Skeleton className="h-60" />
            <Skeleton className="h-60" />
          </div>
        }
      >
        {(comparison) => <ComparisonContent comparison={comparison} />}
      </QueryState>
    </div>
  );
}
