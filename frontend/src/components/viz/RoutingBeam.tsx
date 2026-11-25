"use client";

import { Radio, Unplug } from "lucide-react";
import type { ObservedRouterState } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Live picture of what the router is actually serving (observed state, not the
 * DB's desired state): traffic flows from the router to each target, with beam
 * width and speed proportional to weight. Inspired by MagicUI's Animated Beam,
 * reduced to SVG + CSS so it costs nothing when idle.
 */
export function RoutingBeam({ observed, stableVersion }: { observed: ObservedRouterState | undefined; stableVersion?: string }) {
  const targets = observed?.targets ?? [];
  const total = targets.reduce((s, t) => s + t.weight, 0) || 1;
  const rows = targets.map((t, i) => ({
    ...t,
    pct: (t.weight / total) * 100,
    stable: stableVersion ? t.version === stableVersion : i === 0,
  }));

  const H = 168;
  const routerY = H / 2;
  const slot = H / Math.max(rows.length, 1);

  if (observed && !observed.reachable) {
    return (
      <div className="flex h-[168px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-danger/40 text-sm">
        <Unplug className="size-5 text-danger" />
        <p className="font-medium text-danger">Router unreachable</p>
        <p className="text-xs text-muted-foreground">Actual traffic state unknown until it comes back.</p>
      </div>
    );
  }
  if (!observed || rows.length === 0) {
    return (
      <div className="flex h-[168px] items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
        {observed ? "Router is up but not serving any split yet." : "Reading router state…"}
      </div>
    );
  }

  return (
    <div className="relative grid h-[168px] grid-cols-[auto_1fr_auto] items-center gap-0">
      <div className="z-10 flex flex-col items-center gap-1.5">
        <span className="flex size-12 items-center justify-center rounded-xl border bg-card shadow-sm">
          <Radio className="size-5 text-success" />
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">router · r{observed.revision}</span>
      </div>

      <svg viewBox={`0 0 100 ${H}`} preserveAspectRatio="none" className="h-full w-full" aria-hidden>
        {rows.map((row, i) => {
          const y = slot * i + slot / 2;
          const d = `M0,${routerY} C40,${routerY} 60,${y} 100,${y}`;
          const width = row.pct === 0 ? 0.6 : 1 + (row.pct / 100) * 5;
          const color = row.stable ? "var(--stable)" : "var(--canary)";
          return (
            <g key={row.version}>
              <path d={d} fill="none" stroke="var(--border)" strokeWidth={width + 1} vectorEffect="non-scaling-stroke" />
              {row.pct > 0 ? (
                <path
                  d={d}
                  fill="none"
                  stroke={color}
                  strokeWidth={width}
                  strokeLinecap="round"
                  vectorEffect="non-scaling-stroke"
                  className="beam-flow"
                  style={{ animationDuration: `${Math.max(0.8, 3.2 - (row.pct / 100) * 2.4)}s` }}
                />
              ) : null}
            </g>
          );
        })}
      </svg>

      <div className="z-10 flex h-full flex-col justify-around">
        {rows.map((row) => (
          <div
            key={row.version}
            className={cn(
              "flex min-w-40 items-center justify-between gap-3 rounded-lg border bg-card px-3 py-2 shadow-sm",
              row.pct === 0 && "opacity-50",
            )}
          >
            <span className="flex items-center gap-2">
              <span className={cn("size-2 rounded-full", row.stable ? "bg-stable" : "bg-canary")} />
              <span className="font-mono text-xs">{row.version}</span>
            </span>
            <span className="font-mono text-sm font-medium tabular">{row.pct.toFixed(0)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}
