"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowRight, ArrowUpRight, CircleHelp, GitBranch, Rocket, Undo2 } from "lucide-react";
import { isLive, useDeployments, useFleetSummary, useRouterObserved } from "@/lib/queries";
import { decisionOf, formatDuration, relativeTime } from "@/lib/derive";
import { STATUS_META, TONE_DOT } from "@/lib/status";
import type { DeploymentOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { PageHeader } from "@/components/PageHeader";
import { QueryState } from "@/components/QueryState";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { ActorBadge } from "@/components/viz/ActorBadge";
import { DecisionPanel } from "@/components/viz/DecisionPanel";
import { KpiCard } from "@/components/viz/KpiCard";
import { RolloutLanes } from "@/components/viz/RolloutLanes";
import { FleetModels, fleetModels } from "@/components/viz/FleetModels";
import { SparkBars } from "@/components/viz/SparkBars";

function percent(value: number | null) {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

/** Headline numbers come from GET /api/fleet/summary, computed server-side next to
 * the code that writes the events they're derived from. */
function Kpis() {
  const { data: summary } = useFleetSummary();
  if (!summary) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-[142px]" />
        ))}
      </div>
    );
  }
  const { outcomes, daily } = summary;

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <KpiCard
        label="Live rollouts"
        icon={Rocket}
        tone="text-canary"
        value={summary.live}
        emphasis={summary.live > 0}
        hint={`median time to decision ${formatDuration(summary.median_decision_seconds)}`}
      />
      <KpiCard
        label="Promoted"
        icon={ArrowUpRight}
        tone="text-success"
        value={outcomes.PROMOTED.total}
        hint={`${outcomes.PROMOTED.automatic} by policy engine`}
        chart={<SparkBars values={daily.map((d) => d.PROMOTED)} barClassName="bg-success" title="Promotions, last 14 days" />}
      />
      <KpiCard
        label="Rolled back"
        icon={Undo2}
        tone="text-danger"
        value={outcomes.ROLLED_BACK.total}
        hint={`${outcomes.ROLLED_BACK.automatic} by policy engine · ${percent(summary.automation_rate)} of decisions automated`}
        chart={<SparkBars values={daily.map((d) => d.ROLLED_BACK)} barClassName="bg-danger" title="Rollbacks, last 14 days" />}
      />
      <KpiCard
        label="Inconclusive"
        icon={CircleHelp}
        tone="text-warning"
        value={outcomes.INCONCLUSIVE.total}
        hint="frozen, waiting for a human"
        chart={<SparkBars values={daily.map((d) => d.INCONCLUSIVE)} barClassName="bg-warning" title="Inconclusive, last 14 days" />}
      />
    </div>
  );
}

/** Every model at once, read from the router itself. */
function Models({
  deployments,
  focusId,
  onFocus,
}: {
  deployments: DeploymentOut[];
  focusId: string | undefined;
  onFocus: (deployment: DeploymentOut) => void;
}) {
  const { data: router } = useRouterObserved();
  const models = fleetModels(deployments, router);
  const live = models.filter((m) => isLive(m.latest?.status)).length;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Models</CardTitle>
        <CardDescription>
          What the router is serving right now, for every model
          {models.length ? ` · ${models.length} model${models.length === 1 ? "" : "s"}` : ""}
          {live ? `, ${live} rolling out` : ""}.
        </CardDescription>
        <CardAction>
          <Button asChild variant="ghost" size="xs">
            <Link href="/gateway">
              Gateway <ArrowRight />
            </Link>
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        <FleetModels models={models} reachable={router?.reachable} focusId={focusId} onFocus={onFocus} />
      </CardContent>
    </Card>
  );
}

/** The rollout in focus, with a switcher when several are in flight. */
function Inspector({
  focus,
  live,
  picked,
  onFocus,
}: {
  focus: DeploymentOut;
  live: DeploymentOut[];
  picked: boolean;
  onFocus: (deployment: DeploymentOut) => void;
}) {
  return (
    <Card className="xl:sticky xl:top-16 xl:self-start">
      <CardHeader>
        <CardDescription>
          {isLive(focus.status) ? (live.length > 1 ? `In flight · ${live.length} rollouts` : "In flight") : picked ? "Decision" : "Latest decision"}
        </CardDescription>
        {live.length > 1 ? (
          <div className="flex flex-wrap gap-1 pt-1" role="tablist" aria-label="Rollouts in flight">
            {live.map((d) => (
              <button
                key={d.id}
                type="button"
                role="tab"
                aria-selected={d.id === focus.id}
                onClick={() => onFocus(d)}
                className={cn(
                  "max-w-full truncate rounded-md border px-2 py-0.5 font-mono text-[11px] transition-colors",
                  d.id === focus.id ? "border-canary/50 bg-canary/10 text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {d.model_name}
              </button>
            ))}
          </div>
        ) : null}
      </CardHeader>
      <CardContent>
        <DecisionPanel deployment={focus} />
      </CardContent>
    </Card>
  );
}

function DecisionFeed({ deployments }: { deployments: DeploymentOut[] }) {
  const items = deployments
    .map((d) => ({ d, decision: decisionOf(d) }))
    .filter((x) => x.decision)
    .sort((a, b) => (b.decision?.at?.getTime() ?? 0) - (a.decision?.at?.getTime() ?? 0))
    .slice(0, 6);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent decisions</CardTitle>
        <CardAction>
          <Button asChild variant="ghost" size="xs">
            <Link href="/deployments">
              All <ArrowRight />
            </Link>
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <p className="text-sm text-muted-foreground">No rollout has reached a decision yet.</p>
        ) : (
          <ul className="-mx-2">
            {items.map(({ d, decision }, i) => (
              <li key={d.id} className="animate-in fade-in slide-in-from-bottom-1 fill-mode-both" style={{ animationDelay: `${i * 40}ms` }}>
                <Link href={`/deployments/${d.id}`} className="flex items-center gap-3 rounded-md px-2 py-2 transition-colors hover:bg-accent">
                  <span className={cn("size-2 shrink-0 rounded-full", TONE_DOT[STATUS_META[d.status].tone])} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-mono text-sm">{d.model_name}</span>
                    <span className="block truncate font-mono text-xs text-muted-foreground">
                      {d.stable_version} → {d.canary_version}
                    </span>
                  </span>
                  <span className="hidden sm:block">
                    <ActorBadge actor={decision?.actor ?? null} by={decision?.by} />
                  </span>
                  <StatusBadge status={d.status} />
                  <span className="w-16 text-right font-mono text-xs text-muted-foreground tabular">
                    {relativeTime(decision?.at ?? null)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function OverviewContent({ deployments }: { deployments: DeploymentOut[] }) {
  const [focusId, setFocusId] = useState<string | null>(null);
  if (deployments.length === 0) {
    return (
      <Empty className="border">
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <GitBranch />
          </EmptyMedia>
          <EmptyTitle>No rollouts yet</EmptyTitle>
          <EmptyDescription>
            Pick a candidate model version and send it a slice of live traffic. The control loop takes it from there.
          </EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button asChild size="sm">
            <Link href="/models">Start a rollout</Link>
          </Button>
        </EmptyContent>
      </Empty>
    );
  }

  // Inspector focus: what was clicked, else the first live rollout, else the latest decision.
  const live = deployments.filter((d) => isLive(d.status));
  const focus =
    deployments.find((d) => d.id === focusId) ??
    live[0] ??
    [...deployments].sort((a, b) => (decisionOf(b)?.at?.getTime() ?? 0) - (decisionOf(a)?.at?.getTime() ?? 0))[0];
  const onFocus = (d: DeploymentOut) => setFocusId(d.id);

  return (
    <div className="space-y-6">
      <Kpis />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-6">
          <Models deployments={deployments} focusId={focus?.id} onFocus={onFocus} />
          <Card>
            <CardHeader>
              <CardTitle>Rollout history</CardTitle>
              <CardDescription>Each bar is one rollout, coloured by how it ended.</CardDescription>
            </CardHeader>
            <CardContent>
              <RolloutLanes deployments={deployments} />
            </CardContent>
          </Card>
          <DecisionFeed deployments={deployments} />
        </div>
        <Inspector focus={focus} live={live} picked={focus.id === focusId} onFocus={onFocus} />
      </div>
    </div>
  );
}

export default function OverviewPage() {
  const query = useDeployments();
  return (
    <div className="space-y-6">
      <PageHeader title="Overview" description="What the control loop is doing and what it decided." />
      <QueryState
        query={query}
        skeleton={
          <div className="space-y-6">
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              {Array.from({ length: 4 }, (_, i) => (
                <Skeleton key={i} className="h-[142px]" />
              ))}
            </div>
            <Skeleton className="h-64" />
          </div>
        }
      >
        {(deployments) => <OverviewContent deployments={deployments} />}
      </QueryState>
    </div>
  );
}
