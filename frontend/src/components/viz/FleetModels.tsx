"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Radio, Unplug } from "lucide-react";
import { isLive, observedFor } from "@/lib/queries";
import { parseApiDate } from "@/lib/format";
import type { DeploymentOut, GatewayView, ObservedRouter } from "@/lib/types";
import { cn } from "@/lib/utils";

export type FleetModel = {
  name: string;
  /** The model's most recent rollout, live or finished. */
  latest: DeploymentOut | undefined;
  targets: { version: string; weight: number }[] | null;
};

/** When the rollout last changed: its latest event, or its creation. */
function lastChange(d: DeploymentOut | undefined): Date | null {
  if (!d) return null;
  return parseApiDate(d.events.length ? d.events[d.events.length - 1].created_at : d.created_at);
}

/** One entry per model the router serves or that has a rollout in flight, live
 * rollouts first, then by most recent activity. */
export function fleetModels(deployments: DeploymentOut[], router: ObservedRouter | undefined): FleetModel[] {
  const latest = new Map<string, DeploymentOut>();
  for (const d of deployments) {
    const seen = latest.get(d.model_name);
    if (!seen || d.created_at > seen.created_at) latest.set(d.model_name, d);
  }
  const names = new Set(router?.configs.map((c) => c.model_name) ?? []);
  for (const [name, d] of latest) if (isLive(d.status)) names.add(name);

  const activity = (m: FleetModel) => lastChange(m.latest)?.getTime() ?? 0;
  return [...names]
    .map((name) => ({
      name,
      latest: latest.get(name),
      targets: router ? observedFor(router, name).targets : null,
    }))
    .sort((a, b) => {
      const live = Number(isLive(b.latest?.status)) - Number(isLive(a.latest?.status));
      return live || activity(b) - activity(a) || a.name.localeCompare(b.name);
    });
}

export type ActiveModel = FleetModel & { rps: number };

/** The models that are doing something: a rollout in flight (frozen ones still
 * serve a split) or traffic through the gateway in its last window. */
export function activeModels(models: FleetModel[], gateway: GatewayView | undefined): ActiveModel[] {
  const rps = new Map<string, number>();
  const seen = new Set<string>();
  for (const m of gateway?.models ?? []) {
    rps.set(m.model_name, m.versions.reduce((s, v) => s + v.requests_per_second, 0));
    if (m.versions.some((v) => v.window_requests > 0 || v.in_flight > 0)) seen.add(m.model_name);
  }
  return models
    .filter((m) => isLive(m.latest?.status) || m.latest?.status === "INCONCLUSIVE" || seen.has(m.name))
    .map((m) => ({ ...m, rps: rps.get(m.name) ?? 0 }));
}

const SLOT = 92;
const MAX_PACKETS = 7;

const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

function usePrefersReducedMotion() {
  return useSyncExternalStore(
    (onChange) => {
      const query = window.matchMedia(REDUCED_MOTION);
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    },
    () => window.matchMedia(REDUCED_MOTION).matches,
    () => false,
  );
}

/** Real pixel width of an element, so the SVG can be drawn unstretched: packets
 * are circles, and a non-uniformly scaled viewBox would squash them. */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/**
 * One gateway, every active model: beams fan out from the router to each model
 * it is routing for right now, and each model's card shows the split it serves
 * (read from the router itself). Packets travel each beam at a rate that
 * follows the model's request rate - canary-coloured in proportion to the
 * canary's share - and beam width follows its share of the gateway's traffic.
 * Clicking a model puts its rollout in the inspector.
 */
export function RouterFanout({
  models,
  reachable,
  revisionTotal,
  focusId,
  onFocus,
}: {
  models: ActiveModel[];
  reachable: boolean | undefined;
  revisionTotal: number;
  focusId: string | undefined;
  onFocus: (deployment: DeploymentOut) => void;
}) {
  const reduced = usePrefersReducedMotion();
  const [beamRef, W] = useWidth<HTMLDivElement>();

  if (reachable === false) {
    return (
      <div className="flex h-[168px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-danger/40 text-sm">
        <Unplug className="size-5 text-danger" />
        <p className="font-medium text-danger">Router unreachable</p>
        <p className="text-xs text-muted-foreground">Actual traffic state unknown until it comes back.</p>
      </div>
    );
  }
  if (models.length === 0) {
    return (
      <div className="flex h-[168px] flex-col items-center justify-center gap-1 rounded-lg border border-dashed text-sm text-muted-foreground">
        <p>{reachable === undefined ? "Reading router state…" : "No model is rolling out or taking traffic right now."}</p>
        {revisionTotal ? <p className="text-xs">The router holds a split for {revisionTotal} models; they light up here when traffic arrives.</p> : null}
      </div>
    );
  }

  const H = Math.max(184, models.length * SLOT);
  const totalRps = models.reduce((s, m) => s + m.rps, 0);
  const rows = models.map((m, i) => {
    const total = m.targets?.reduce((s, t) => s + t.weight, 0) || 1;
    // Only a model whose traffic is actually split has a canary; one serving a
    // single version (a promoted one included) is all stable.
    const serving = (m.targets ?? []).filter((t) => t.weight > 0).length;
    const split = (m.targets ?? []).map((t, j) => ({
      version: t.version,
      pct: (t.weight / total) * 100,
      stable: serving <= 1 ? t.weight > 0 || j === 0 : m.latest ? t.version === m.latest.stable_version : j === 0,
    }));
    const canaryPct = split.filter((p) => !p.stable).reduce((s, p) => s + p.pct, 0);
    const share = totalRps > 0 ? m.rps / totalRps : 1 / models.length;
    const y = (H / models.length) * i + H / models.length / 2;
    return { ...m, i, y, split, share, canaryPct, canary: canaryPct > 0 };
  });

  const path = (y: number) => `M0,${H / 2} C${W * 0.45},${H / 2} ${W * 0.55},${y} ${W},${y}`;

  return (
    <div
      className="relative grid grid-cols-[auto_1fr_auto] items-center rounded-xl bg-[radial-gradient(var(--border)_1px,transparent_1px)] [background-size:18px_18px] px-1"
      style={{ height: H }}
      data-testid="router-fanout"
    >
      {/* The gateway: every request enters here. */}
      <div className="relative z-10 flex flex-col items-center gap-2">
        <span className="relative flex size-16 items-center justify-center">
          {totalRps > 0 && !reduced ? (
            <>
              <span className="absolute inset-0 animate-ping rounded-2xl bg-success/20 [animation-duration:2.4s]" />
              <span className="absolute -inset-2 rounded-3xl bg-success/10 blur-md" />
            </>
          ) : null}
          <span className="relative flex size-16 items-center justify-center rounded-2xl border border-success/30 bg-card shadow-lg shadow-success/10">
            <Radio className="size-6 text-success" />
          </span>
        </span>
        <span className="text-center leading-tight">
          <span className="block font-mono text-lg font-semibold tabular">{totalRps.toFixed(1)}</span>
          <span className="block text-[10px] tracking-wide text-muted-foreground uppercase">req/s · gateway</span>
        </span>
      </div>

      <div ref={beamRef} className="h-full min-w-0">
        {W > 0 ? (
          <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="overflow-visible" aria-hidden>
            <defs>
              <filter id="beam-glow" x="-10%" y="-50%" width="120%" height="200%">
                <feGaussianBlur stdDeviation="3" />
              </filter>
              {rows.map((row) => (
                <linearGradient key={row.name} id={`beam-${row.i}`} gradientUnits="userSpaceOnUse" x1={0} y1={H / 2} x2={W} y2={row.y}>
                  <stop offset="0%" stopColor="var(--success)" stopOpacity={0.55} />
                  <stop offset="100%" stopColor={row.canary ? "var(--canary)" : "var(--stable)"} />
                </linearGradient>
              ))}
            </defs>
            {rows.map((row) => {
              const d = path(row.y);
              const width = 1.5 + row.share * 5;
              const idle = row.rps === 0;
              const packets = idle ? 0 : Math.min(MAX_PACKETS, Math.max(2, Math.round(row.rps)));
              // Faster with more traffic; one trip takes 2.6s at idle-ish rates, 1.1s when busy.
              const dur = Math.max(1.1, 2.6 - Math.min(row.rps, 15) / 10);
              return (
                <g key={row.name} opacity={idle ? 0.45 : 1}>
                  <path d={d} fill="none" stroke="var(--border)" strokeWidth={width + 2} strokeLinecap="round" />
                  {!idle ? (
                    <path d={d} fill="none" stroke={`url(#beam-${row.i})`} strokeWidth={width + 4} strokeLinecap="round" opacity={0.35} filter="url(#beam-glow)" />
                  ) : null}
                  <path
                    d={d}
                    fill="none"
                    stroke={`url(#beam-${row.i})`}
                    strokeWidth={width}
                    strokeLinecap="round"
                    strokeDasharray={idle ? "3 6" : undefined}
                  />
                  {!reduced
                    ? Array.from({ length: packets }, (_, k) => {
                        const canaryPacket = (k + 0.5) / packets < row.canaryPct / 100;
                        return (
                          <circle key={k} r={2.6} fill={canaryPacket ? "var(--canary)" : "var(--stable)"} stroke="var(--card)" strokeWidth={1}>
                            <animateMotion dur={`${dur}s`} begin={`${(-dur * k) / packets}s`} repeatCount="indefinite" path={d} />
                          </circle>
                        );
                      })
                    : null}
                </g>
              );
            })}
          </svg>
        ) : null}
      </div>

      <ul className="z-10 grid h-full" style={{ gridTemplateRows: `repeat(${rows.length}, minmax(0, 1fr))` }}>
        {rows.map((row) => {
          const focused = row.latest?.id === focusId;
          return (
            <li key={row.name} className="flex items-center" data-model={row.name}>
              <button
                type="button"
                disabled={!row.latest}
                onClick={row.latest ? () => onFocus(row.latest!) : undefined}
                className={cn(
                  "group relative w-80 max-w-full overflow-hidden rounded-xl border bg-card/95 py-3 pr-4 pl-5 text-left shadow-sm backdrop-blur transition-all duration-200 enabled:hover:-translate-y-0.5 enabled:hover:shadow-md",
                  focused && "border-canary/60 shadow-md shadow-canary/10 ring-1 ring-canary/30",
                )}
              >
                {/* State rail: blue while traffic is split, grey when one version serves it all. */}
                <span className={cn("absolute inset-y-0 left-0 w-1", row.canary ? "bg-canary" : "bg-stable/60")} />
                <span className="flex items-center justify-between gap-3">
                  <span className="flex min-w-0 items-center gap-1.5 font-mono text-xs font-medium">
                    <span className="truncate" title={row.name}>{row.name}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-1 font-mono text-[10px] text-muted-foreground tabular">
                    {row.rps > 0 ? (
                      <>
                        <span className="relative flex size-1.5">
                          {!reduced ? <span className="absolute inset-0 animate-ping rounded-full bg-success/60" /> : null}
                          <span className="relative size-1.5 rounded-full bg-success" />
                        </span>
                        {row.rps.toFixed(1)} req/s
                      </>
                    ) : (
                      "idle"
                    )}
                  </span>
                </span>
                <span className="mt-2 flex h-2 gap-0.5 overflow-hidden rounded-full" aria-hidden>
                  {row.split
                    .filter((p) => p.pct > 0)
                    .map((p) => (
                      <span
                        key={p.version}
                        className={cn(
                          "h-full rounded-full transition-[width] duration-700",
                          p.stable ? "bg-stable" : "bg-canary shadow-[0_0_8px_var(--canary)]",
                        )}
                        style={{ width: `${p.pct}%` }}
                      />
                    ))}
                </span>
                <span className="mt-1.5 flex items-baseline justify-between gap-3 font-mono text-[10px] text-muted-foreground tabular">
                  {row.split.map((p) => (
                    <span key={p.version} className={cn("truncate", !p.stable && "text-canary")}>
                      {p.version} <span className={cn("font-semibold", !p.stable ? "text-canary" : "text-foreground")}>{Math.round(p.pct)}%</span>
                    </span>
                  ))}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
