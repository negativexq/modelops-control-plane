"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowUpRight,
  Brain,
  CircleCheck,
  CircleHelp,
  CircleX,
  FlaskConical,
  Gauge,
  Loader2,
  OctagonAlert,
  Play,
  Timer,
  TriangleAlert,
  Undo2,
  X,
  type LucideIcon,
} from "lucide-react";
import { TONE_PILL, type Tone } from "@/lib/status";
import { getBenchmarkRun, getCurrentBenchmarkRun } from "@/lib/api";
import { keys, useBenchmarkRuns, useScenarios, useStartBenchmark } from "@/lib/queries";
import { formatDate, parseApiDate } from "@/lib/format";
import type { BenchmarkRun, ScenarioInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { PageHeader } from "@/components/PageHeader";
import { QueryState } from "@/components/QueryState";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

function formatSeconds(value: number | null): string {
  return value == null ? "N/A" : `${value.toFixed(1)}s`;
}

function elapsedSince(startedAt: string): string {
  const seconds = Math.max(0, (Date.now() - parseApiDate(startedAt).getTime()) / 1000);
  return `${Math.floor(seconds)}s`;
}

const RUN_TONE: Record<BenchmarkRun["status"], string> = {
  RUNNING: "text-canary",
  COMPLETED: "text-success",
  FAILED: "text-danger",
};

export default function BenchmarksPage() {
  const scenarios = useScenarios();
  const history = useBenchmarkRuns();
  const start = useStartBenchmark();
  const [viewedRunId, setViewedRunId] = useState<string | null>(null);

  // Picks up an already-running benchmark (another tab, or a reload mid-run), and
  // keeps polling while it runs. Stops once it reaches a terminal status but keeps
  // that terminal result on screen.
  const current = useQuery({
    queryKey: keys.benchmarkCurrent,
    queryFn: getCurrentBenchmarkRun,
    refetchInterval: (query) => (query.state.data?.status === "RUNNING" ? 2_000 : 10_000),
  });
  const viewed = useQuery({
    queryKey: ["benchmarks", "run", viewedRunId],
    queryFn: () => getBenchmarkRun(viewedRunId as string),
    enabled: viewedRunId != null,
  });

  const activeRun = current.data ?? null;
  const isRunning = activeRun?.status === "RUNNING";
  const displayedRun = viewedRunId ? viewed.data : activeRun && !isRunning ? activeRun : null;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Benchmarks"
        description="Drive a real canary rollout under synthetic load and check the control loop reacts as expected. One run at a time, since the router holds a single active split."
      />

      {isRunning && activeRun ? (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border border-canary/30 bg-canary/8 px-4 py-3 text-sm">
          <span className="inline-flex items-center gap-2 font-medium">
            <span className="live-dot size-2 rounded-full bg-canary text-canary" aria-hidden />
            Running <span className="font-mono">{activeRun.scenario}</span>
          </span>
          <span className="font-mono text-xs text-muted-foreground tabular">
            elapsed {elapsedSince(activeRun.started_at)}
          </span>
          <span className="font-mono text-xs text-muted-foreground tabular">
            started {formatDate(activeRun.started_at)}
          </span>
        </div>
      ) : null}

      {displayedRun ? (
        <BenchmarkResultCard
          run={displayedRun}
          onDismiss={viewedRunId ? () => setViewedRunId(null) : undefined}
        />
      ) : viewedRunId && viewed.isPending ? (
        <Card>
          <CardContent className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Fetching result…
          </CardContent>
        </Card>
      ) : null}

      <QueryState query={scenarios}>
        {(list) => (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {list.map((scenario) => (
              <ScenarioCard
                key={scenario.key}
                scenario={scenario}
                disabled={isRunning || start.isPending}
                submitting={start.isPending && start.variables?.scenario === scenario.key}
                onRun={() => {
                  setViewedRunId(null);
                  start.mutate({ scenario: scenario.key });
                }}
              />
            ))}
          </div>
        )}
      </QueryState>

      {history.data && history.data.length > 0 ? (
        <Card className="py-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-4">Scenario</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead className="pr-4">Completed</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {history.data.slice(0, 10).map((run) => (
                <TableRow
                  key={run.id}
                  onClick={() => run.status !== "RUNNING" && setViewedRunId(run.id)}
                  data-state={viewedRunId === run.id ? "selected" : undefined}
                  className={run.status !== "RUNNING" ? "cursor-pointer" : undefined}
                >
                  <TableCell className="pl-4 font-mono">{run.scenario}</TableCell>
                  <TableCell className={cn("text-xs font-medium", RUN_TONE[run.status])}>{run.status}</TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground tabular">
                    {formatDate(run.started_at)}
                  </TableCell>
                  <TableCell className="pr-4 font-mono text-xs text-muted-foreground tabular">
                    {formatDate(run.completed_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      ) : null}
    </div>
  );
}

const SCENARIO_ICON: Record<string, LucideIcon> = {
  baseline: Gauge,
  "latency-failure": Timer,
  "error-failure": OctagonAlert,
  "quality-failure": Brain,
  success: ArrowUpRight,
};

/** Expected outcome as a toned pill, so the five cards read as "what should the
 * control loop do here" at a glance. Unknown outcomes fall back to neutral. */
function expectation(outcome: string): { tone: Tone; icon: LucideIcon } {
  if (outcome.includes("rollback")) return { tone: "danger", icon: Undo2 };
  if (outcome.includes("promote")) return { tone: "success", icon: ArrowUpRight };
  if (outcome.includes("inconclusive")) return { tone: "warning", icon: CircleHelp };
  return { tone: "neutral", icon: Gauge };
}

function ScenarioCard({
  scenario,
  disabled,
  submitting,
  onRun,
}: {
  scenario: ScenarioInfo;
  disabled: boolean;
  submitting: boolean;
  onRun: () => void;
}) {
  return (
    <Card size="sm" className="group justify-between transition-colors hover:border-foreground/20">
      <CardHeader>
        <div className="mb-1 flex size-8 items-center justify-center rounded-lg border bg-muted/40 text-muted-foreground transition-colors group-hover:text-foreground">
          {(() => {
            const Icon = SCENARIO_ICON[scenario.key] ?? FlaskConical;
            return <Icon className="size-4" />;
          })()}
        </div>
        <CardTitle>{scenario.title}</CardTitle>
        <CardDescription className="line-clamp-3">{scenario.description}</CardDescription>
      </CardHeader>
      <CardFooter className="flex items-center justify-between gap-2">
        {(() => {
          const e = expectation(scenario.expected_outcome);
          return (
            <span
              className={`inline-flex h-5 items-center gap-1 rounded-full border px-2 font-mono text-[11px] ${TONE_PILL[e.tone]}`}
              title="Expected outcome"
            >
              <e.icon className="size-3" />
              {scenario.expected_outcome}
            </span>
          );
        })()}
        <div className="flex items-center gap-1.5">
          {scenario.synthetic_disclaimer ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="text-warning" aria-label="Synthetic-label disclaimer">
                  <TriangleAlert className="size-4" />
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-sm">{scenario.synthetic_disclaimer}</TooltipContent>
            </Tooltip>
          ) : null}
          <Button size="xs" variant="outline" onClick={onRun} disabled={disabled}>
            {submitting ? <Loader2 className="animate-spin" /> : <Play />}
            Run
          </Button>
        </div>
      </CardFooter>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-muted/30 px-3 py-2">
      <p className="text-[11px] tracking-wide text-muted-foreground uppercase">{label}</p>
      <p className="font-mono text-sm tabular">{value}</p>
    </div>
  );
}

function BenchmarkResultCard({ run, onDismiss }: { run: BenchmarkRun; onDismiss?: () => void }) {
  const result = run.result;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FlaskConical className="size-4 text-muted-foreground" />
          Result · <span className="font-mono">{run.scenario}</span>
          {result ? (
            result.outcome_matches_expectation ? (
              <span className="inline-flex items-center gap-1 text-xs font-medium text-success">
                <CircleCheck className="size-3.5" /> matched expectation
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-xs font-medium text-danger">
                <CircleX className="size-3.5" /> did not match
              </span>
            )
          ) : null}
        </CardTitle>
        {onDismiss ? (
          <CardAction>
            <Button size="icon-xs" variant="ghost" onClick={onDismiss} aria-label="Close result">
              <X />
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {run.status === "FAILED" ? (
          <div className="rounded-md border border-danger/30 bg-danger/8 px-3 py-2">
            <p className="font-medium text-danger">The benchmark subprocess failed.</p>
            {run.error_message ? (
              <pre className="mt-2 max-h-40 overflow-auto font-mono text-xs whitespace-pre-wrap text-muted-foreground">
                {run.error_message}
              </pre>
            ) : null}
          </div>
        ) : null}

        {result ? (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat label="Expected" value={result.expected_outcome} />
              <Stat label="Observed" value={result.observed_outcome} />
              <Stat label="Time to detect" value={formatSeconds(result.time_to_detect_seconds)} />
              <Stat label="Time to action" value={formatSeconds(result.time_to_action_seconds)} />
              {result.load ? (
                <>
                  <Stat label="Requests" value={String(result.load.total_requests)} />
                  <Stat label="Failures" value={String(result.load.total_failures)} />
                  <Stat label="Error rate" value={`${(result.load.error_rate * 100).toFixed(2)}%`} />
                  <Stat label="p95" value={`${result.load.p95_ms.toFixed(1)} ms`} />
                </>
              ) : null}
            </div>
            {result.notes.length > 0 ? (
              <ul className="list-inside list-disc space-y-1 text-muted-foreground">
                {result.notes.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            ) : null}
          </>
        ) : run.status === "COMPLETED" ? (
          <p className="text-muted-foreground">Run completed, but no JSON report was found.</p>
        ) : null}
      </CardContent>
    </Card>
  );
}
