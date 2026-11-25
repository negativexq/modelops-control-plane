"use client";

import Link from "next/link";
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
import { RoutingBeam } from "@/components/viz/RoutingBeam";
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
          <Skeleton key={i} className="h-[118px]" />
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

function LiveRouting({ deployments }: { deployments: DeploymentOut[] }) {
  const { data: observed } = useRouterObserved();
  const served = deployments.find((d) => d.id === observed?.deployment_id);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Live routing</CardTitle>
        <CardDescription>What the router is serving right now, read from the router itself.</CardDescription>
        {served ? (
          <CardAction>
            <Button asChild variant="ghost" size="xs">
              <Link href={`/deployments/${served.id}`}>
                <span className="font-mono">{served.model_name}</span> <ArrowRight />
              </Link>
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent>
        <RoutingBeam observed={observed} stableVersion={served?.stable_version} />
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
                    <ActorBadge actor={decision?.actor ?? null} />
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

  // Inspector focus: the live rollout if there is one, else the latest decision.
  const focus =
    deployments.find((d) => isLive(d.status)) ??
    [...deployments].sort((a, b) => (decisionOf(b)?.at?.getTime() ?? 0) - (decisionOf(a)?.at?.getTime() ?? 0))[0];

  return (
    <div className="space-y-6">
      <Kpis />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-6">
          <LiveRouting deployments={deployments} />
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
        <Card className="xl:sticky xl:top-16 xl:self-start">
          <CardHeader>
            <CardDescription>{isLive(focus.status) ? "In flight" : "Latest decision"}</CardDescription>
          </CardHeader>
          <CardContent>
            <DecisionPanel deployment={focus} />
          </CardContent>
        </Card>
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
                <Skeleton key={i} className="h-28" />
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
