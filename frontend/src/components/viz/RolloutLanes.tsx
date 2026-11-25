"use client";

import Link from "next/link";
import { parseApiDate } from "@/lib/format";
import { decisionOf, formatDuration, durationSeconds } from "@/lib/derive";
import { STATUS_META, TONE_DOT } from "@/lib/status";
import type { DeploymentOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * Gantt-style history (after Kibo UI's Gantt): one lane per rollout on a shared
 * time axis, each bar coloured by how it ended. Makes "how long do rollouts take
 * and how do they end" readable at a glance.
 */
export function RolloutLanes({ deployments, limit = 8 }: { deployments: DeploymentOut[]; limit?: number }) {
  const now = useNow();
  const rows = deployments
    .filter((d) => d.started_at)
    .slice(0, limit)
    .map((d) => {
      const start = parseApiDate(d.started_at as string).getTime();
      const end = d.completed_at ? parseApiDate(d.completed_at).getTime() : now;
      return { d, start, end };
    })
    .reverse();
  if (rows.length === 0) return null;

  const min = Math.min(...rows.map((r) => r.start));
  const max = Math.max(...rows.map((r) => r.end));
  const span = Math.max(1, max - min);
  const fmt = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  return (
    <div className="space-y-1.5">
      {rows.map(({ d, start, end }) => {
        const meta = STATUS_META[d.status];
        const left = ((start - min) / span) * 100;
        const width = Math.max(1.2, ((end - start) / span) * 100);
        const decision = decisionOf(d);
        return (
          <div key={d.id} className="grid grid-cols-[minmax(0,11rem)_1fr] items-center gap-3">
            <Link href={`/deployments/${d.id}`} className="truncate font-mono text-xs text-muted-foreground hover:text-foreground">
              {d.model_name}
            </Link>
            <div className="relative h-5 rounded bg-muted/40">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Link
                    href={`/deployments/${d.id}`}
                    className={cn(
                      "absolute top-1 h-3 rounded-sm opacity-85 transition-opacity hover:opacity-100",
                      TONE_DOT[meta.tone],
                      !d.completed_at && "animate-pulse",
                    )}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    aria-label={`${d.model_name}: ${meta.label}`}
                  />
                </TooltipTrigger>
                <TooltipContent>
                  <p className="font-mono">
                    {d.model_name} · {d.canary_version}
                  </p>
                  <p>
                    {meta.label}
                    {decision?.actor ? ` · ${decision.actor}` : ""} · {formatDuration(durationSeconds(d))}
                  </p>
                </TooltipContent>
              </Tooltip>
            </div>
          </div>
        );
      })}
      <div className="grid grid-cols-[minmax(0,11rem)_1fr] gap-3 pt-1">
        <span />
        <div className="flex justify-between font-mono text-[10px] text-muted-foreground tabular">
          <span>{fmt(min)}</span>
          <span>{fmt(min + span / 2)}</span>
          <span>{fmt(max)}</span>
        </div>
      </div>
    </div>
  );
}
