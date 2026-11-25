"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import * as api from "./api";
import { streamState } from "./live-stream";
import { keys } from "./query-keys";
import type { DeploymentOut, DeploymentStatus } from "./types";

/**
 * Single source of truth for server state. Replaces the old hand-rolled
 * useAsync/useMutation pair. The contract those hooks enforced still holds:
 * nothing is updated optimistically. Every mutation waits for the control
 * plane's answer and then invalidates, so what's on screen is always what was
 * persisted, never a local guess. What's new is polling: a canary in flight
 * changes underneath the operator (worker advances traffic, policy checks land),
 * and a control plane UI that needs F5 to notice is lying by omission.
 */

// Statuses where the worker/router may still change things on their own.
export const LIVE_STATUSES: ReadonlySet<DeploymentStatus> = new Set([
  "PENDING",
  "DEPLOYING",
  "CANARY_RUNNING",
  "EVALUATING",
  "PROMOTING",
  "ROLLING_BACK",
]);

export const isLive = (status: DeploymentStatus | undefined) =>
  status != null && LIVE_STATUSES.has(status);

const LIVE_INTERVAL_MS = 3_000;
// Short even when idle: a rollout started from another tab or the CLI should
// show up about as fast as the router state (polled every 6s) does.
const IDLE_INTERVAL_MS = 5_000;
// While the change stream (live-stream.ts) is open it drives refetches; polling
// only remains as a safety net in case an event is ever missed.
const STREAMING_INTERVAL_MS = 30_000;

/** Poll interval for data the change stream covers (deployments, events,
 * policy results). Not for metrics or router state: those aren't streamed. */
function streamedInterval(fastMs: number | false): number | false {
  if (fastMs === false) return false;
  return streamState() === "open" ? STREAMING_INTERVAL_MS : fastMs;
}

export { keys };

// --- Deployments ----------------------------------------------------------------

export function useDeployments() {
  return useQuery({
    queryKey: keys.deployments,
    queryFn: api.listDeployments,
    refetchInterval: (query) =>
      streamedInterval(query.state.data?.some((d) => isLive(d.status)) ? LIVE_INTERVAL_MS : IDLE_INTERVAL_MS),
  });
}

export function useFleetSummary() {
  return useQuery({
    queryKey: keys.fleetSummary,
    queryFn: api.getFleetSummary,
    refetchInterval: () => streamedInterval(IDLE_INTERVAL_MS),
  });
}

export function useDeployment(id: string, enabled = true) {
  return useQuery({
    queryKey: keys.deployment(id),
    queryFn: () => api.getDeployment(id),
    enabled: enabled && id.length > 0,
    refetchInterval: (query) => streamedInterval(isLive(query.state.data?.status) ? LIVE_INTERVAL_MS : false),
  });
}

export function useTimeline(id: string, live: boolean) {
  return useQuery({
    queryKey: keys.timeline(id),
    queryFn: () => api.getDeploymentTimeline(id),
    refetchInterval: () => streamedInterval(live ? LIVE_INTERVAL_MS : false),
  });
}

export function useComparison(id: string, windowSeconds: number, live: boolean) {
  return useQuery({
    queryKey: keys.comparison(id, windowSeconds),
    queryFn: () => api.getDeploymentComparison(id, windowSeconds),
    refetchInterval: live ? LIVE_INTERVAL_MS * 2 : false,
    placeholderData: (previous) => previous,
  });
}

export function useRouterObserved(enabled = true) {
  return useQuery({
    queryKey: keys.routerObserved,
    queryFn: api.getObservedRouterState,
    refetchInterval: LIVE_INTERVAL_MS * 2,
    enabled,
  });
}

// --- Models ---------------------------------------------------------------------

export function useModels() {
  return useQuery({ queryKey: keys.models, queryFn: api.getModels, staleTime: 60_000 });
}

export function useModelVersions(model: string) {
  return useQuery({
    queryKey: keys.modelVersions(model),
    queryFn: () => api.getModelVersions(model),
    enabled: model.length > 0,
    staleTime: 60_000,
  });
}

export function useModelVersionRows(model: string) {
  return useQuery({
    queryKey: keys.modelVersionRows(model),
    staleTime: 60_000,
    queryFn: async () => {
      const versions = await api.getModelVersions(model);
      return Promise.all(
        versions.map(async (version) => {
          const [metadata, evaluation] = await Promise.all([
            api.getModelVersionMetadata(model, version),
            api.getModelVersionEvaluation(model, version),
          ]);
          return { version, metadata, evaluation };
        }),
      );
    },
  });
}

// --- Benchmarks -----------------------------------------------------------------

export function useScenarios() {
  return useQuery({ queryKey: keys.scenarios, queryFn: api.getScenarios, staleTime: Infinity });
}

export function useBenchmarkRuns() {
  return useQuery({
    queryKey: keys.benchmarkRuns,
    queryFn: api.listBenchmarkRuns,
    refetchInterval: (query) =>
      query.state.data?.some((run) => run.status === "RUNNING") ? LIVE_INTERVAL_MS : IDLE_INTERVAL_MS,
  });
}

// --- Mutations ------------------------------------------------------------------

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

type DeploymentAction = "promote" | "rollback" | "pause" | "resume";

const ACTIONS: Record<
  DeploymentAction,
  { call: (id: string) => Promise<DeploymentOut>; success: string; failure: string }
> = {
  promote: {
    call: api.promoteDeployment,
    success: "Promoted — canary now serves 100% of traffic.",
    failure: "Promote failed",
  },
  rollback: {
    call: api.rollbackDeployment,
    success: "Rolled back — stable now serves 100% of traffic.",
    failure: "Rollback failed",
  },
  pause: { call: api.pauseAutomation, success: "Automation paused.", failure: "Pause failed" },
  resume: { call: api.resumeAutomation, success: "Automation resumed.", failure: "Resume failed" },
};

export function useDeploymentAction(action: DeploymentAction) {
  const queryClient = useQueryClient();
  const spec = ACTIONS[action];
  return useMutation({
    mutationFn: (id: string) => spec.call(id),
    onSuccess: async (deployment) => {
      toast.success(spec.success, {
        description: `${deployment.model_name} · ${deployment.stable_version} → ${deployment.canary_version}`,
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.deployments }),
        queryClient.invalidateQueries({ queryKey: keys.fleetSummary }),
        queryClient.invalidateQueries({ queryKey: keys.routerObserved }),
      ]);
    },
    onError: (error) => toast.error(spec.failure, { description: errorMessage(error) }),
  });
}

export function useCreateDeployment() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof api.createDeployment>[0]) =>
      api.createDeployment(payload, crypto.randomUUID()),
    onSuccess: async (deployment) => {
      toast.success("Canary deployment started", {
        description: `${deployment.model_name} · ${deployment.canary_version}`,
      });
      await queryClient.invalidateQueries({ queryKey: keys.deployments });
    },
    onError: (error) => toast.error("Could not start deployment", { description: errorMessage(error) }),
  });
}

export function useStartBenchmark() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: api.startBenchmarkRun,
    onSuccess: async (run) => {
      toast.success("Benchmark started", { description: run.scenario });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["benchmarks"] }),
        queryClient.invalidateQueries({ queryKey: keys.deployments }),
      ]);
    },
    onError: (error) => toast.error("Could not start benchmark", { description: errorMessage(error) }),
  });
}
