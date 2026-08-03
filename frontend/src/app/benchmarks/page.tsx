"use client";

import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  getBenchmarkRun,
  getCurrentBenchmarkRun,
  getScenarios,
  listBenchmarkRuns,
  startBenchmarkRun,
} from "@/lib/api";
import { useAsync } from "@/lib/useAsync";
import { AsyncBoundary } from "@/components/AsyncBoundary";
import { Card } from "@/components/Card";
import { ResultBanner } from "@/components/ResultBanner";
import { formatDate, parseApiDate } from "@/lib/format";
import type { BenchmarkRun, ScenarioInfo } from "@/lib/types";

const POLL_INTERVAL_MS = 2000;

function formatSeconds(value: number | null): string {
  if (value == null) return "N/A";
  return `${value.toFixed(1)}s`;
}

function elapsedSince(startedAt: string): string {
  const seconds = Math.max(0, (Date.now() - parseApiDate(startedAt).getTime()) / 1000);
  return `${Math.floor(seconds)}s`;
}

export default function BenchmarksPage() {
  const {
    data: scenarios,
    error: scenariosError,
    loading: scenariosLoading,
  } = useAsync(() => getScenarios());

  const {
    data: history,
    refetch: refetchHistory,
  } = useAsync(() => listBenchmarkRuns());

  const [activeRun, setActiveRun] = useState<BenchmarkRun | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [startingScenario, setStartingScenario] = useState<string | null>(null);
  // A run picked from "Recent runs" to inspect - separate from `activeRun` (the
  // live/just-finished run) so that reloading the page and clicking an older row
  // still shows its full result, not just the summary table.
  const [viewedRun, setViewedRun] = useState<BenchmarkRun | null>(null);
  const [viewedRunLoading, setViewedRunLoading] = useState(false);

  const activeRunRef = useRef<BenchmarkRun | null>(null);
  useEffect(() => {
    activeRunRef.current = activeRun;
  }, [activeRun]);

  // Picks up an already-running benchmark (e.g. started from another tab, or the
  // page was reloaded mid-run) rather than assuming nothing is in flight.
  useEffect(() => {
    let cancelled = false;
    getCurrentBenchmarkRun()
      .then((run) => {
        if (!cancelled) setActiveRun(run);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setInitialized(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Polls GET /api/benchmarks/current (or the specific run once we know its id) while
  // a run is RUNNING; stops re-fetching once it reaches a terminal status, but keeps
  // that terminal result on screen.
  useEffect(() => {
    if (!initialized) return;
    const interval = setInterval(() => {
      const current = activeRunRef.current;
      if (current && current.status !== "RUNNING") return;
      const poll = current ? getBenchmarkRun(current.id) : getCurrentBenchmarkRun();
      poll
        .then((run) => {
          setActiveRun(run);
          if (run && run.status !== "RUNNING") refetchHistory();
        })
        .catch(() => {});
    }, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [initialized, refetchHistory]);

  const isRunning = activeRun?.status === "RUNNING";

  async function handleRun(scenarioKey: string) {
    setStartError(null);
    setStartingScenario(scenarioKey);
    try {
      const run = await startBenchmarkRun({ scenario: scenarioKey });
      setActiveRun(run);
      setViewedRun(null);
    } catch (error) {
      setStartError(error instanceof ApiError ? error.message : String(error));
    } finally {
      setStartingScenario(null);
    }
  }

  async function handleViewRun(runId: string) {
    setViewedRunLoading(true);
    try {
      setViewedRun(await getBenchmarkRun(runId));
    } catch {
      // best-effort - the row itself already shows status/timestamps
    } finally {
      setViewedRunLoading(false);
    }
  }

  const terminalActiveRun = activeRun && activeRun.status !== "RUNNING" ? activeRun : null;
  const displayedResult = terminalActiveRun ?? viewedRun;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Benchmarks</h1>
        <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
          Runs a canary rollout under synthetic load against one of five scenarios. Only one
          benchmark can run at a time - the router has a single active traffic split.
        </p>
      </div>

      {isRunning && activeRun ? (
        <Card title="Benchmark running">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
            <span className="inline-flex items-center gap-2">
              <span
                className="h-2 w-2 animate-pulse rounded-full bg-blue-500"
                aria-hidden
              />
              <span className="font-medium text-zinc-900 dark:text-zinc-100">
                {activeRun.scenario}
              </span>
            </span>
            <span className="text-zinc-600 dark:text-zinc-400">
              Elapsed: {elapsedSince(activeRun.started_at)}
            </span>
            <span className="text-zinc-600 dark:text-zinc-400">
              Started: {formatDate(activeRun.started_at)}
            </span>
          </div>
        </Card>
      ) : null}

      {startError ? (
        <ResultBanner
          result={{ kind: "error", message: startError }}
          onDismiss={() => setStartError(null)}
        />
      ) : null}

      {displayedResult ? (
        <BenchmarkResultCard
          run={displayedResult}
          onDismiss={terminalActiveRun ? undefined : () => setViewedRun(null)}
        />
      ) : viewedRunLoading ? (
        <Card title="Loading run…">
          <p className="text-sm text-zinc-500 dark:text-zinc-400">Fetching result…</p>
        </Card>
      ) : null}

      <AsyncBoundary data={scenarios} error={scenariosError} loading={scenariosLoading}>
        {(scenarioList) => (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {scenarioList.map((scenario) => (
              <ScenarioCard
                key={scenario.key}
                scenario={scenario}
                disabled={isRunning}
                submitting={startingScenario === scenario.key}
                runningScenario={activeRun?.scenario ?? null}
                onRun={() => handleRun(scenario.key)}
              />
            ))}
          </div>
        )}
      </AsyncBoundary>

      {history && history.length > 0 ? (
        <Card title="Recent runs">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-left text-sm">
              <thead>
                <tr className="border-b border-zinc-200 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
                  <th className="py-2 pr-4">Scenario</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Started</th>
                  <th className="py-2 pr-4">Completed</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100 dark:divide-zinc-900">
                {history.slice(0, 10).map((run) => (
                  <tr
                    key={run.id}
                    onClick={() => run.status !== "RUNNING" && handleViewRun(run.id)}
                    className={
                      run.status !== "RUNNING"
                        ? "cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-900"
                        : undefined
                    }
                  >
                    <td className="py-2 pr-4">{run.scenario}</td>
                    <td className="py-2 pr-4">{run.status}</td>
                    <td className="py-2 pr-4 whitespace-nowrap text-zinc-600 dark:text-zinc-400">
                      {formatDate(run.started_at)}
                    </td>
                    <td className="py-2 pr-4 whitespace-nowrap text-zinc-600 dark:text-zinc-400">
                      {formatDate(run.completed_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
    </div>
  );
}

function ScenarioCard({
  scenario,
  disabled,
  submitting,
  runningScenario,
  onRun,
}: {
  scenario: ScenarioInfo;
  disabled: boolean;
  submitting: boolean;
  runningScenario: string | null;
  onRun: () => void;
}) {
  return (
    <Card title={scenario.title}>
      <div className="space-y-3 text-sm">
        <p className="text-zinc-700 dark:text-zinc-300">{scenario.description}</p>
        <p className="text-zinc-600 dark:text-zinc-400">
          <span className="font-medium text-zinc-700 dark:text-zinc-300">Expected: </span>
          {scenario.expected_outcome}
        </p>
        {scenario.synthetic_disclaimer ? (
          <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {scenario.synthetic_disclaimer}
          </p>
        ) : null}
        <div>
          <button
            type="button"
            onClick={onRun}
            disabled={disabled || submitting}
            className="rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-zinc-100 dark:text-black dark:hover:bg-zinc-300"
          >
            {submitting ? "Starting…" : "Run"}
          </button>
          {disabled ? (
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
              {runningScenario
                ? `A benchmark (${runningScenario}) is already running - wait for it to finish.`
                : "A benchmark is already running - wait for it to finish."}
            </p>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

function BenchmarkResultCard({
  run,
  onDismiss,
}: {
  run: BenchmarkRun;
  onDismiss?: () => void;
}) {
  const result = run.result;
  return (
    <Card
      title={`Result: ${run.scenario}`}
      action={
        onDismiss ? (
          <button
            type="button"
            onClick={onDismiss}
            className="text-xs font-medium text-zinc-500 underline-offset-2 hover:underline dark:text-zinc-400"
          >
            Close
          </button>
        ) : undefined
      }
    >
      <div className="space-y-3 text-sm">
        {run.status === "FAILED" ? (
          <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
            <p className="font-medium">The benchmark subprocess failed.</p>
            {run.error_message ? (
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-xs">
                {run.error_message}
              </pre>
            ) : null}
          </div>
        ) : null}

        {result ? (
          <>
            <div className="flex flex-wrap gap-x-6 gap-y-1">
              <span>
                <span className="font-medium">Expected: </span>
                {result.expected_outcome}
              </span>
              <span>
                <span className="font-medium">Observed: </span>
                {result.observed_outcome}
              </span>
              <span>
                {result.outcome_matches_expectation ? "✅ matches expectation" : "❌ did not match"}
              </span>
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-zinc-600 dark:text-zinc-400">
              <span>Time to detect: {formatSeconds(result.time_to_detect_seconds)}</span>
              <span>Time to action: {formatSeconds(result.time_to_action_seconds)}</span>
            </div>
            {result.load ? (
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-zinc-600 dark:text-zinc-400">
                <span>Requests: {result.load.total_requests}</span>
                <span>Failures: {result.load.total_failures}</span>
                <span>Error rate: {(result.load.error_rate * 100).toFixed(2)}%</span>
                <span>p95: {result.load.p95_ms.toFixed(1)} ms</span>
              </div>
            ) : null}
            {result.notes.length > 0 ? (
              <ul className="list-inside list-disc text-zinc-600 dark:text-zinc-400">
                {result.notes.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            ) : null}
          </>
        ) : run.status === "COMPLETED" ? (
          <p className="text-zinc-500 dark:text-zinc-400">
            Run completed, but no JSON report was found.
          </p>
        ) : null}
      </div>
    </Card>
  );
}
