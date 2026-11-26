"use client";

import Link from "next/link";
import { ArrowRight, Unplug } from "lucide-react";
import { isLive, observedFor } from "@/lib/queries";
import { relativeTime } from "@/lib/derive";
import { parseApiDate } from "@/lib/format";
import type { DeploymentOut, ObservedRouter } from "@/lib/types";
import { cn } from "@/lib/utils";
import { StatusBadge } from "@/components/StatusBadge";

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

/** What the router serves for one model, as one stacked bar: stable grey, canary blue. */
function SplitBar({ targets, stableVersion }: { targets: FleetModel["targets"]; stableVersion?: string }) {
  if (!targets || targets.length === 0) {
    return <span className="text-xs text-muted-foreground">not routed</span>;
  }
  const total = targets.reduce((s, t) => s + t.weight, 0) || 1;
  const parts = targets.map((t, i) => ({
    ...t,
    pct: (t.weight / total) * 100,
    stable: stableVersion ? t.version === stableVersion : i === 0,
  }));
  return (
    <div className="min-w-0 space-y-1">
      <div className="flex h-2 overflow-hidden rounded-full bg-muted" aria-hidden>
        {parts.map((p) => (
          <span
            key={p.version}
            className={cn("h-full transition-[width] duration-700", p.stable ? "bg-stable" : "bg-canary")}
            style={{ width: `${p.pct}%` }}
          />
        ))}
      </div>
      <p className="truncate font-mono text-[11px] text-muted-foreground tabular">
        {parts.map((p, i) => (
          <span key={p.version}>
            {i > 0 ? " · " : null}
            <span className={p.stable ? undefined : "text-canary"}>{p.version}</span> {Math.round(p.pct)}%
          </span>
        ))}
      </p>
    </div>
  );
}

/**
 * Every model at once: what the router serves for each (read from the router,
 * not the database), its rollout and when it last changed. Clicking a row puts
 * that model's rollout in the inspector; the arrow opens it.
 */
export function FleetModels({
  models,
  reachable,
  focusId,
  onFocus,
}: {
  models: FleetModel[];
  reachable: boolean | undefined;
  focusId: string | undefined;
  onFocus: (deployment: DeploymentOut) => void;
}) {
  return (
    <div className="space-y-2">
      {reachable === false ? (
        <p className="flex items-center gap-2 rounded-md border border-dashed border-danger/40 px-3 py-2 text-xs text-danger">
          <Unplug className="size-3.5" /> Router unreachable: the splits below are unknown until it answers.
        </p>
      ) : null}
      {models.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          {reachable === undefined ? "Reading router state…" : "The router isn't serving any model yet."}
        </p>
      ) : (
        <ul className="-mx-2" data-testid="fleet-models">
          <li className="hidden grid-cols-[minmax(0,15rem)_minmax(0,1fr)_8.5rem_4.5rem_1.5rem] gap-4 px-2 pb-1 text-[11px] tracking-wide text-muted-foreground uppercase md:grid">
            <span>Model</span>
            <span>Serving now</span>
            <span>Rollout</span>
            <span className="text-right">Changed</span>
            <span />
          </li>
          {models.map(({ name, latest, targets }) => {
            const focused = latest != null && latest.id === focusId;
            const changed = relativeTime(lastChange(latest));
            return (
              <li
                key={name}
                data-model={name}
                className={cn(
                  "grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-2 rounded-md px-2 py-2 transition-colors md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)_8.5rem_4.5rem_1.5rem]",
                  latest && "cursor-pointer hover:bg-accent",
                  focused && "bg-accent",
                )}
                onClick={latest ? () => onFocus(latest) : undefined}
              >
                <span className="min-w-0">
                  <span className="flex items-center gap-1.5 font-mono text-sm" title={name}>
                    {isLive(latest?.status) ? <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-canary" /> : null}
                    <span className="truncate">{name}</span>
                  </span>
                  {latest ? (
                    <span className="block truncate font-mono text-xs text-muted-foreground">
                      {latest.stable_version} → {latest.canary_version}
                    </span>
                  ) : null}
                </span>
                <span className="order-last col-span-2 md:order-none md:col-span-1">
                  <SplitBar targets={targets} stableVersion={latest?.stable_version} />
                </span>
                <span className="justify-self-end md:justify-self-start">
                  {latest ? <StatusBadge status={latest.status} /> : <span className="text-xs text-muted-foreground">no rollout</span>}
                </span>
                <span className="hidden text-right font-mono text-xs text-muted-foreground tabular md:block">{changed}</span>
                <span className="hidden md:block">
                  {latest ? (
                    <Link
                      href={`/deployments/${latest.id}`}
                      aria-label={`Open ${name} rollout`}
                      onClick={(e) => e.stopPropagation()}
                      className="flex size-6 items-center justify-center rounded text-muted-foreground hover:bg-background hover:text-foreground"
                    >
                      <ArrowRight className="size-3.5" />
                    </Link>
                  ) : null}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
