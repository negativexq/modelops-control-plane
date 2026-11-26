"use client";

import type { CSSProperties } from "react";
import { Cpu, Gauge, Radio, ShieldAlert, ShieldCheck, ShieldHalf, Users, Waypoints } from "lucide-react";
import type { DeploymentOut, GatewayModel, GatewayPolicy, GatewayVersion } from "@/lib/types";
import { formatMs } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * One model's request path through the gateway, drawn from the router's live
 * stats (GET /api/router/gateway): clients -> gateway -> each version.
 *
 * Beams: width is the configured share of
 * traffic, speed is the measured request rate, colour is the version's role.
 * A version whose circuit breaker is open gets a red, stopped beam - the
 * gateway is failing those requests fast instead of sending them.
 */

const LANE_MIN_H = 176;
const SLOT_H = 92;

/** Seconds per dash cycle: idle beams crawl, busy ones run. */
export function beamDuration(rps: number) {
  if (rps <= 0) return 0;
  return Math.max(0.45, 3 - Math.log10(rps + 1) * 1.3);
}

function roleOf(version: string, deployment: DeploymentOut | undefined, index: number) {
  // A promotion makes the canary the new stable. Any other state (live, frozen
  // INCONCLUSIVE, rolled back) keeps the deployment's own roles.
  if (deployment?.status === "PROMOTED") return "stable";
  if (deployment) {
    if (version === deployment.canary_version) return "canary";
    if (version === deployment.stable_version) return "stable";
  }
  return index === 0 ? "stable" : "canary";
}

const BREAKER = {
  closed: { icon: ShieldCheck, label: "closed", className: "text-success" },
  half_open: { icon: ShieldHalf, label: "half-open", className: "text-warning" },
  open: { icon: ShieldAlert, label: "open", className: "text-danger" },
} as const;

function Beam({
  from,
  to,
  height,
  width,
  color,
  rps,
  broken,
  idle,
}: {
  from: number;
  to: number;
  height: number;
  width: number;
  color: string;
  rps: number;
  broken?: boolean;
  idle?: boolean;
}) {
  const d = `M0,${from} C45,${from} 55,${to} 100,${to}`;
  const duration = beamDuration(rps);
  return (
    <g>
      <path d={d} fill="none" stroke="var(--border)" strokeWidth={width + 1} vectorEffect="non-scaling-stroke" />
      {broken ? (
        <path d={d} fill="none" stroke="var(--danger)" strokeWidth={width} strokeDasharray="2 6" vectorEffect="non-scaling-stroke" />
      ) : idle ? null : (
        <path
          d={d}
          fill="none"
          stroke={color}
          strokeWidth={width}
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
          className={duration ? "beam-flow" : undefined}
          strokeOpacity={duration ? 1 : 0.35}
          style={duration ? { animationDuration: `${duration}s` } : undefined}
        />
      )}
      {/* Height marker so preserveAspectRatio="none" keeps a stable vertical scale. */}
      <rect x={0} y={0} width={0} height={height} fill="none" />
    </g>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="min-w-0">
      <p className="text-[10px] tracking-wide text-muted-foreground uppercase">{label}</p>
      <p className={cn("font-mono text-xs tabular", tone)}>{value}</p>
    </div>
  );
}

function pct(v: number | null | undefined, digits = 1) {
  return v == null ? "—" : `${(v * 100).toFixed(digits)}%`;
}

function VersionNode({ v, role }: { v: GatewayVersion; role: "stable" | "canary" }) {
  const breaker = BREAKER[v.breaker];
  const failing = v.outcomes.server_error + v.outcomes.gateway_error + v.outcomes.rejected;
  return (
    <div
      className={cn(
        "relative w-full rounded-lg border bg-card px-3 py-2 shadow-sm",
        v.breaker === "open" && "border-danger/50",
        v.weight === 0 && "opacity-55",
      )}
      data-testid={`gateway-version-${v.version}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <span className={cn("size-2 shrink-0 rounded-full", role === "stable" ? "bg-stable" : "bg-canary")} />
          <span className="truncate font-mono text-xs">{v.version}</span>
          <span className="text-[10px] text-muted-foreground">{role}</span>
        </span>
        <span className="font-mono text-sm font-medium tabular">{(v.weight * 100).toFixed(0)}%</span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1.5">
        <Stat label="req/s" value={v.requests_per_second.toFixed(1)} />
        <Stat label="p95" value={v.p95_latency_ms == null ? "—" : formatMs(v.p95_latency_ms)} />
        <Stat label="errors" value={pct(v.error_rate)} tone={failing > 0 ? "text-danger" : undefined} />
        <div className="min-w-0" title={`Circuit breaker ${breaker.label}; opened ${v.breaker_opened_total}× since the router started`}>
          <p className="text-[10px] tracking-wide text-muted-foreground uppercase">breaker</p>
          <p className={cn("flex items-center gap-1 font-mono text-xs whitespace-nowrap", breaker.className)}>
            <breaker.icon className="size-3" />
            {breaker.label}
          </p>
        </div>
      </div>
      {v.in_flight > 0 ? (
        <span className="absolute -top-2 right-2 rounded-full border bg-background px-1.5 font-mono text-[10px] text-muted-foreground tabular">
          {v.in_flight} in flight
        </span>
      ) : null}
    </div>
  );
}

export function GatewayFlow({
  model,
  policy,
  deployment,
}: {
  model: GatewayModel;
  policy: GatewayPolicy;
  deployment: DeploymentOut | undefined;
}) {
  const versions = model.versions;
  const H = Math.max(LANE_MIN_H, versions.length * SLOT_H);
  const mid = H / 2;
  const slot = H / Math.max(versions.length, 1);
  const totalRps = versions.reduce((s, v) => s + v.requests_per_second, 0);
  const windowTotal = versions.reduce((s, v) => s + v.window_requests, 0);
  const anyOpen = versions.some((v) => v.breaker === "open");

  // min-height, not height: the row grows with its tallest node instead of
  // clipping it, and the beams (preserveAspectRatio="none") stretch along with
  // it. In a card narrower than the row needs (container query), the nodes stack
  // and the beams are dropped rather than squashed or scrolled out of view.
  return (
    <div className="@container">
      <div
        className="flex flex-col gap-3 @2xl:grid @2xl:min-h-(--lane-h) @2xl:grid-cols-[minmax(7.5rem,auto)_minmax(2rem,1fr)_minmax(11rem,auto)_minmax(3rem,1.4fr)_minmax(16rem,1.6fr)] @2xl:items-stretch @2xl:gap-0"
        style={{ "--lane-h": `${H}px` } as CSSProperties}
        data-testid={`gateway-flow-${model.model_name}`}
      >
        {/* Clients */}
        <div className="z-10 self-start rounded-lg border @2xl:self-center bg-card px-3 py-2 shadow-sm">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <Users className="size-3.5 text-muted-foreground" /> Clients
          </p>
          <p className="mt-1 font-mono text-lg leading-none font-semibold tabular">{totalRps.toFixed(1)}</p>
          <p className="text-[10px] text-muted-foreground">req/s · last 10s</p>
          {model.sticky_requests_total > 0 ? (
            <p className="mt-1 text-[10px] text-muted-foreground" title="Requests with X-Routing-Key, pinned to one version by weighted rendezvous hashing">
              {model.sticky_requests_total} sticky
            </p>
          ) : null}
        </div>

        <div className="relative hidden @2xl:block">
        <svg viewBox={`0 0 100 ${H}`} preserveAspectRatio="none" className="absolute inset-0 h-full w-full" aria-hidden>
          <Beam from={mid} to={mid} height={H} width={2 + Math.min(4, totalRps / 5)} color="var(--foreground)" rps={totalRps} idle={windowTotal === 0} />
        </svg>
        </div>

        {/* Gateway */}
        <div className={cn("z-10 self-start rounded-xl border @2xl:self-center bg-card px-3 py-2.5 shadow-sm", anyOpen && "border-danger/40")}>
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <Radio className="size-3.5 text-success" /> Gateway
            <span className="ml-auto font-mono text-[10px] text-muted-foreground">r{model.revision}</span>
          </p>
          <ul className="mt-1.5 space-y-1 text-[11px] text-muted-foreground">
            <li className="flex items-center gap-1.5" title="Token bucket per model; over budget answers 429 with Retry-After">
              <Gauge className="size-3" />
              {policy.rate_limit_rps > 0 ? `${policy.rate_limit_rps} req/s limit` : "no rate limit"}
              {model.rate_limited_total > 0 ? <span className="text-warning">· {model.rate_limited_total} limited</span> : null}
            </li>
            <li className="flex items-center gap-1.5" title="Consecutive unreachable/not-ready answers that open a version's breaker, and how long it stays open">
              <ShieldCheck className="size-3" />
              breaker {policy.breaker_failure_threshold}× / {policy.breaker_cooldown_seconds}s
            </li>
            <li className="flex items-center gap-1.5" title="Same X-Routing-Key, same version - and ramping the canary only moves keys onto it">
              <Waypoints className="size-3" /> sticky by X-Routing-Key
            </li>
          </ul>
        </div>

        <div className="relative hidden @2xl:block">
        <svg viewBox={`0 0 100 ${H}`} preserveAspectRatio="none" className="absolute inset-0 h-full w-full" aria-hidden>
          {versions.map((v, i) => (
            <Beam
              key={v.version}
              from={mid}
              to={slot * i + slot / 2}
              height={H}
              width={v.weight === 0 ? 0.6 : 1 + v.weight * 5}
              color={roleOf(v.version, deployment, i) === "stable" ? "var(--stable)" : "var(--canary)"}
              rps={v.requests_per_second}
              broken={v.breaker === "open"}
              idle={v.weight === 0}
            />
          ))}
        </svg>
        </div>

        <div className="z-10 flex flex-col justify-around gap-2 py-1">
          {versions.map((v, i) => (
            <VersionNode key={v.version} v={v} role={roleOf(v.version, deployment, i)} />
          ))}
        </div>

      </div>
    </div>
  );
}

export function GatewayLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <span className="h-0.5 w-5 rounded bg-stable" /> stable
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-0.5 w-5 rounded bg-canary" /> canary
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-0.5 w-5 border-t-2 border-dotted border-danger" /> breaker open (failing fast)
      </span>
      <span className="flex items-center gap-1.5">
        <Cpu className="size-3" /> width = traffic share, speed = request rate
      </span>
    </div>
  );
}
