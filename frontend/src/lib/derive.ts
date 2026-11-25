import { parseApiDate } from "./format";
import type { DeploymentOut, DeploymentStatus, TimelineItem, TimelinePolicyItem } from "./types";

/**
 * Read-only views derived from data the API already returns (DeploymentOut.events,
 * the timeline). Nothing here invents state: when the event history can't answer
 * a question (e.g. who made a decision), the answer is `null`, not a guess.
 */

export type Outcome = "PROMOTED" | "ROLLED_BACK" | "INCONCLUSIVE" | "FAILED";
export type Actor = "automatic" | "manual";

export const OUTCOMES: readonly Outcome[] = ["PROMOTED", "ROLLED_BACK", "INCONCLUSIVE", "FAILED"];

export interface Decision {
  outcome: Outcome;
  actor: Actor | null;
  /** Operator name, when a person made the call and said who they were. */
  by: string | null;
  at: Date | null;
}

const OUTCOME_STATUSES: ReadonlySet<DeploymentStatus> = new Set(OUTCOMES);

/** The decision a rollout ended in, plus who made it. The backend tags the
 * final status_changed message with "(automatic)" or "(manual)"; INCONCLUSIVE is
 * always reached by the worker. */
export function decisionOf(deployment: DeploymentOut): Decision | null {
  if (!OUTCOME_STATUSES.has(deployment.status)) return null;
  const outcome = deployment.status as Outcome;
  let actor: Actor | null = null;
  let by: string | null = null;
  if (outcome === "INCONCLUSIVE") {
    actor = "automatic";
  } else {
    const final = [...deployment.events]
      .reverse()
      .find((e) => e.event_type === "status_changed" && e.message.includes(`-> ${outcome}`));
    if (final?.message.includes("(automatic)")) actor = "automatic";
    else if (final?.message.includes("(manual)")) actor = "manual";
    if (actor === "manual") by = final?.actor ?? null;
  }
  const at = deployment.completed_at
    ? parseApiDate(deployment.completed_at)
    : deployment.events.length
      ? parseApiDate(deployment.events[deployment.events.length - 1].created_at)
      : null;
  return { outcome, actor, by, at };
}

export function durationSeconds(deployment: DeploymentOut): number | null {
  if (!deployment.started_at) return null;
  const end = deployment.completed_at ? parseApiDate(deployment.completed_at) : new Date();
  return Math.max(0, (end.getTime() - parseApiDate(deployment.started_at).getTime()) / 1000);
}

export function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  const h = Math.floor(seconds / 3600);
  return `${h}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function relativeTime(date: Date | null, now = Date.now()): string {
  if (!date) return "—";
  const s = Math.round((now - date.getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** Canary weight (0-100) currently desired for a deployment. Stable is the
 * target whose version equals stable_version; everything else is canary. */
export function canaryPercent(deployment: DeploymentOut): number | null {
  const targets = deployment.traffic_allocation?.targets;
  if (!targets?.length) return null;
  const total = targets.reduce((sum, t) => sum + t.weight, 0) || 1;
  const canary = targets
    .filter((t) => t.version !== deployment.stable_version)
    .reduce((sum, t) => sum + t.weight, 0);
  return (canary / total) * 100;
}

export interface TrafficPoint {
  t: number; // epoch ms
  canary: number; // 0-100
  label: string;
}

const ADVANCE_RE = /from (\d+(?:\.\d+)?)% to (\d+(?:\.\d+)?)%/;
// Rollouts created since the control plane started recording it carry the
// initial weight on their "-> CANARY_RUNNING" transition.
const INITIAL_RE = /receiving traffic at (\d+(?:\.\d+)?)%/;

/**
 * Canary-weight progression reconstructed from `traffic_advanced` events and the
 * final decision. Returns null when the history doesn't contain enough to draw
 * it honestly: older rollouts don't record their initial weight, so without an
 * advance (or a live allocation) a made-up starting point would be a lie.
 */
export function trafficSeries(deployment: DeploymentOut): TrafficPoint[] | null {
  const advances = deployment.events
    .filter((e) => e.event_type === "traffic_advanced")
    .map((e) => ({ at: parseApiDate(e.created_at).getTime(), m: ADVANCE_RE.exec(e.message) }))
    .filter((e): e is { at: number; m: RegExpExecArray } => e.m !== null);

  const startEvent = deployment.events.find(
    (e) => e.event_type === "status_changed" && e.message.includes("-> CANARY_RUNNING"),
  );
  const start = startEvent
    ? parseApiDate(startEvent.created_at).getTime()
    : deployment.started_at
      ? parseApiDate(deployment.started_at).getTime()
      : null;
  if (start == null) return null;

  const current = canaryPercent(deployment);
  const recorded = startEvent ? INITIAL_RE.exec(startEvent.message) : null;
  const initial = recorded
    ? Number(recorded[1])
    : advances.length
      ? Number(advances[0].m[1])
      : OUTCOME_STATUSES.has(deployment.status)
        ? null
        : current;
  if (initial == null) return null;

  const points: TrafficPoint[] = [{ t: start, canary: initial, label: "canary receiving traffic" }];
  for (const a of advances) points.push({ t: a.at, canary: Number(a.m[2]), label: "auto advance" });

  const decision = decisionOf(deployment);
  if (decision?.at && (decision.outcome === "PROMOTED" || decision.outcome === "ROLLED_BACK")) {
    points.push({
      t: decision.at.getTime(),
      canary: decision.outcome === "PROMOTED" ? 100 : 0,
      label: decision.outcome === "PROMOTED" ? "promoted" : "rolled back",
    });
  } else if (current != null) {
    points.push({ t: Date.now(), canary: current, label: "now" });
  }
  return points;
}

/** Latest result per policy - the evidence the last decision was based on. */
export function latestPolicyResults(items: TimelineItem[]): TimelinePolicyItem[] {
  const byPolicy = new Map<string, TimelinePolicyItem>();
  for (const item of items) {
    if (item.type !== "policy_evaluation") continue;
    const prev = byPolicy.get(item.policy_name);
    if (!prev || parseApiDate(item.timestamp) >= parseApiDate(prev.timestamp)) byPolicy.set(item.policy_name, item);
  }
  const order = { FAIL: 0, INCONCLUSIVE: 1, PASS: 2 } as const;
  return [...byPolicy.values()].sort((a, b) => order[a.result] - order[b.result]);
}
