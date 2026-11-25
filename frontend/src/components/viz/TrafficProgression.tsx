"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { TrafficPoint } from "@/lib/derive";

function Tip({ active, payload }: { active?: boolean; payload?: Array<{ payload: TrafficPoint }> }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="font-mono text-foreground tabular">{p.canary.toFixed(0)}% canary</p>
      <p className="text-muted-foreground">
        {p.label} · {new Date(p.t).toLocaleTimeString()}
      </p>
    </div>
  );
}

/** Canary share over time as a step chart: traffic moves in discrete jumps
 * (auto-advance, promote, rollback), so interpolating between them would lie. */
export function TrafficProgression({ points }: { points: TrafficPoint[] }) {
  return (
    <div className="h-36 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 6, right: 6, bottom: 0, left: -18 }}>
          <defs>
            <linearGradient id="canaryFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--canary)" stopOpacity={0.35} />
              <stop offset="100%" stopColor="var(--canary)" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            tickFormatter={(t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            tickLine={false}
            axisLine={false}
            fontSize={10}
            stroke="var(--muted-foreground)"
            minTickGap={40}
          />
          <YAxis
            domain={[0, 100]}
            ticks={[0, 50, 100]}
            tickFormatter={(v: number) => `${v}%`}
            tickLine={false}
            axisLine={false}
            fontSize={10}
            stroke="var(--muted-foreground)"
          />
          <Tooltip content={<Tip />} cursor={{ stroke: "var(--border)" }} />
          <Area
            type="stepAfter"
            dataKey="canary"
            stroke="var(--canary)"
            strokeWidth={2}
            fill="url(#canaryFill)"
            dot={{ r: 3, fill: "var(--canary)", strokeWidth: 0 }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
