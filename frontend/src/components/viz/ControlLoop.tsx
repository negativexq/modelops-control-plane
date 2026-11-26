"use client";

import { Bot, Database, Radio, Scale } from "lucide-react";
import type { ReactNode } from "react";
import { canaryPercent, latestPolicyResults, relativeTime } from "@/lib/derive";
import { parseApiDate } from "@/lib/format";
import { useTimeline } from "@/lib/queries";
import type { DeploymentOut, GatewayModel, TimelinePolicyItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { beamDuration } from "./GatewayFlow";

/**
 * The loop that steers a rollout, left to right and back: the gateway pushes a
 * metric per request, the control plane windows them, the policy engine tests
 * the canary against its limits, the worker acts on the verdict, and the new
 * split is pushed back to the gateway as the next routing revision.
 */

const RESULT_DOT = {
  PASS: "bg-success",
  FAIL: "bg-danger",
  INCONCLUSIVE: "bg-warning",
} as const;

const STAT_CHECKS: Record<string, { label: string; format: (v: number) => string }> = {
  max_error_rate: { label: "error rate", format: (v) => `${v.toFixed(1)}%` },
  latency_p95_increase: { label: "p95 Δ", format: (v) => `${v >= 0 ? "+" : ""}${v.toFixed(0)}%` },
  minimum_recall: { label: "recall", format: (v) => v.toFixed(2) },
  feature_drift: { label: "drift PSI", format: (v) => v.toFixed(2) },
  prediction_drift: { label: "score PSI", format: (v) => v.toFixed(2) },
};

function Node({ icon: Icon, title, children, tone }: { icon: typeof Radio; title: string; children: ReactNode; tone?: string }) {
  return (
    <div className="z-10 min-w-0 rounded-lg border bg-card px-3 py-2 shadow-sm">
      <p className="flex items-center gap-1.5 text-xs font-medium">
        <Icon className={cn("size-3.5", tone ?? "text-muted-foreground")} /> {title}
      </p>
      <div className="mt-1 space-y-0.5 text-[11px] text-muted-foreground">{children}</div>
    </div>
  );
}

function Link({ rps, label }: { rps: number; label: string }) {
  const duration = beamDuration(rps);
  return (
    <div className="hidden h-full min-w-6 flex-col items-center justify-center gap-1 @3xl:flex">
      <svg viewBox="0 0 100 8" preserveAspectRatio="none" className="h-2 w-full" aria-hidden>
        <path d="M0,4 L100,4" stroke="var(--border)" strokeWidth={3} vectorEffect="non-scaling-stroke" />
        <path
          d="M0,4 L100,4"
          stroke="var(--canary)"
          strokeWidth={2}
          vectorEffect="non-scaling-stroke"
          className={duration ? "beam-flow" : undefined}
          strokeOpacity={duration ? 1 : 0.3}
          style={duration ? { animationDuration: `${duration}s` } : undefined}
        />
      </svg>
      <span className="text-center font-mono text-[10px] leading-tight text-muted-foreground">{label}</span>
    </div>
  );
}

function Check({ item }: { item: TimelinePolicyItem }) {
  const stat = STAT_CHECKS[item.policy_name];
  const value = stat && item.observed_value != null ? stat.format(item.observed_value) : null;
  const ci =
    stat && item.ci_low != null && item.ci_high != null ? `[${stat.format(item.ci_low)}, ${stat.format(item.ci_high)}]` : null;
  return (
    <li className="flex items-center gap-1.5" title={item.explanation}>
      <span className={cn("size-1.5 shrink-0 rounded-full", RESULT_DOT[item.result])} />
      <span className="min-w-0 truncate">{stat?.label ?? item.policy_name.replaceAll("_", " ")}</span>
      {value ? <span className="ml-auto font-mono whitespace-nowrap text-foreground tabular">{value}</span> : null}
      {ci ? <span className="hidden font-mono whitespace-nowrap tabular min-[1400px]:inline">{ci}</span> : null}
    </li>
  );
}

export function ControlLoop({ deployment, model }: { deployment: DeploymentOut; model: GatewayModel | undefined }) {
  const timeline = useTimeline(deployment.id, true);
  const checks = latestPolicyResults(timeline.data ?? []);
  // The tested checks first (they carry an interval), then any gate holding things up.
  const shown = [
    ...checks.filter((c) => c.policy_name in STAT_CHECKS),
    ...checks.filter((c) => !(c.policy_name in STAT_CHECKS) && c.result !== "PASS"),
  ].slice(0, 4);
  const lastEval = checks.length ? Math.max(...checks.map((c) => parseApiDate(c.timestamp).getTime())) : null;
  const rps = model?.versions.reduce((s, v) => s + v.requests_per_second, 0) ?? 0;
  const window = deployment.policy_config?.evaluation_window_seconds;
  const confidence = deployment.policy_config?.confidence_level;
  const canary = canaryPercent(deployment);
  const lastEvent = [...deployment.events].sort((a, b) => parseApiDate(b.created_at).getTime() - parseApiDate(a.created_at).getTime())[0];

  return (
    <div className="space-y-1 @container" data-testid="control-loop">
      {/* Narrow cards (container query): a 2-up grid without the connecting beams. */}
      <div className="grid grid-cols-2 gap-2 @3xl:grid-cols-[minmax(0,1fr)_minmax(2.5rem,0.45fr)_minmax(0,1fr)_minmax(2.5rem,0.45fr)_minmax(0,1.5fr)_minmax(2.5rem,0.45fr)_minmax(0,1.2fr)] @3xl:items-stretch @3xl:gap-0">
        <Node icon={Radio} title="Gateway" tone="text-success">
          <p>one metric per request</p>
          <p className="font-mono text-foreground tabular">{rps.toFixed(1)} /s</p>
        </Node>
        <Link rps={rps} label="metrics" />
        <Node icon={Database} title="Control plane">
          <p>windows the samples</p>
          <p className="font-mono text-foreground tabular">{window ? `${window}s window` : "—"}</p>
        </Node>
        <Link rps={rps > 0 ? 1 : 0} label="evaluate" />
        <Node icon={Scale} title="Policy engine" tone="text-canary">
          {shown.length ? (
            <ul className="space-y-0.5">
              {shown.map((c) => (
                <Check key={c.policy_name} item={c} />
              ))}
            </ul>
          ) : (
            <p>no evaluation yet</p>
          )}
          <p className="pt-0.5 text-[10px]">
            {confidence ? `${Math.round(confidence * 100)}% CI` : ""}
            {lastEval ? ` · ${relativeTime(new Date(lastEval))}` : ""}
          </p>
        </Node>
        <Link rps={rps > 0 ? 1 : 0} label="verdict" />
        <Node icon={Bot} title="Worker">
          <p>
            canary at <span className="font-mono text-foreground tabular">{canary == null ? "—" : `${canary.toFixed(0)}%`}</span>
            {deployment.automation_paused ? <span className="text-warning"> · paused</span> : null}
          </p>
          {lastEvent ? (
            <p className="line-clamp-2" title={lastEvent.message}>
              {lastEvent.event_type.replaceAll("_", " ")} · {relativeTime(parseApiDate(lastEvent.created_at))}
            </p>
          ) : null}
        </Node>
      </div>
      {/* The way back: the worker's decision becomes the gateway's next split. */}
      <div className="relative hidden h-7 @3xl:block">
        <svg viewBox="0 0 100 24" preserveAspectRatio="none" className="absolute inset-0 h-full w-full" aria-hidden>
          <path d="M92,0 L92,14 Q92,20 86,20 L14,20 Q8,20 8,14 L8,0" fill="none" stroke="var(--border)" strokeWidth={3} vectorEffect="non-scaling-stroke" />
          <path
            d="M92,0 L92,14 Q92,20 86,20 L14,20 Q8,20 8,14 L8,0"
            fill="none"
            stroke="var(--stable)"
            strokeWidth={1.5}
            vectorEffect="non-scaling-stroke"
            className="beam-flow"
            style={{ animationDuration: "3s" }}
          />
        </svg>
        <span className="absolute -bottom-px left-1/2 -translate-x-1/2 bg-card px-2 font-mono text-[10px] leading-4 whitespace-nowrap text-muted-foreground">
          config push · routing revision r{model?.revision ?? deployment.traffic_allocation?.revision ?? "—"}
        </span>
      </div>
    </div>
  );
}
