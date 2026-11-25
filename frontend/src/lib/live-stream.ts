"use client";

import { useEffect, useSyncExternalStore } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { STREAM_URL } from "./api";
import { keys } from "./query-keys";
import type { DeploymentOut, StreamChange } from "./types";

/**
 * Push side of the data layer. The control plane streams one Server-Sent Event per
 * new DeploymentEvent / PolicyEvaluation row (GET /api/stream); each one
 * invalidates exactly the queries it affects. Polling stays as the fallback: while
 * the stream is open the poll intervals in queries.ts relax to a slow safety net,
 * and they tighten again the moment it drops.
 */

export type StreamState = "connecting" | "open" | "closed";

let state: StreamState = "connecting";
const listeners = new Set<() => void>();

function setState(next: StreamState) {
  if (next === state) return;
  state = next;
  listeners.forEach((listener) => listener());
}

export function streamState(): StreamState {
  return state;
}

export function useStreamState(): StreamState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
    () => "connecting",
  );
}

const DECISION_RE = /-> (PROMOTED|ROLLED_BACK|INCONCLUSIVE|FAILED)\b/;
// Events that also move what the router should be serving.
const ROUTING_EVENTS = new Set(["status_changed", "traffic_advanced", "router_unreachable", "router_reachable"]);
const FLUSH_MS = 200;

/** Decisions the operator didn't make from this tab are worth a toast; manual
 * actions already got one from their mutation. */
function announce(queryClient: QueryClient, change: Extract<StreamChange, { kind: "event" }>) {
  const outcome = DECISION_RE.exec(change.message)?.[1];
  if (!outcome || change.message.includes("(manual)")) return;
  const deployment = queryClient
    .getQueryData<DeploymentOut[]>(keys.deployments)
    ?.find((d) => d.id === change.deployment_id);
  const name = deployment ? `${deployment.model_name} · ${deployment.canary_version}` : undefined;
  const title =
    outcome === "PROMOTED"
      ? "Policy engine promoted a canary"
      : outcome === "ROLLED_BACK"
        ? "Policy engine rolled back a canary"
        : outcome === "INCONCLUSIVE"
          ? "Rollout is inconclusive: needs a human"
          : "Rollout failed";
  const show = outcome === "PROMOTED" ? toast.success : outcome === "INCONCLUSIVE" ? toast.warning : toast.error;
  show(title, { description: name });
}

/** Open the stream for the lifetime of the app. Mount once (see Providers). */
export function useLiveStream() {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      setState("closed");
      return;
    }

    const source = new EventSource(STREAM_URL);
    const touched = new Set<string>();
    let routing = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let opened = false;

    const flush = () => {
      timer = undefined;
      const ids = [...touched];
      touched.clear();
      void queryClient.invalidateQueries({ queryKey: keys.deployments, exact: true });
      void queryClient.invalidateQueries({ queryKey: keys.fleetSummary });
      for (const id of ids) void queryClient.invalidateQueries({ queryKey: keys.deployment(id) });
      if (routing) void queryClient.invalidateQueries({ queryKey: keys.routerObserved });
      routing = false;
    };

    source.addEventListener("ready", () => {
      setState("open");
      // A reconnect may have missed changes; refetch once rather than guess.
      if (opened) void queryClient.invalidateQueries({ queryKey: keys.deployments });
      opened = true;
    });

    source.addEventListener("change", (message) => {
      let change: StreamChange;
      try {
        change = JSON.parse((message as MessageEvent<string>).data) as StreamChange;
      } catch {
        return;
      }
      touched.add(change.deployment_id);
      if (change.kind === "event") {
        if (ROUTING_EVENTS.has(change.event_type)) routing = true;
        if (change.event_type === "status_changed") announce(queryClient, change);
      }
      timer ??= setTimeout(flush, FLUSH_MS);
    });

    source.onerror = () => {
      setState(source.readyState === EventSource.CLOSED ? "closed" : "connecting");
    };

    return () => {
      source.close();
      if (timer) clearTimeout(timer);
      setState("closed");
    };
  }, [queryClient]);
}
