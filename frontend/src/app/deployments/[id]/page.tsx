"use client";

import { useParams } from "next/navigation";
import { CircleAlert, CircleCheck, Loader2, Unplug } from "lucide-react";
import { isLive, observedFor, useDeployment, useRouterObserved, useTimeline } from "@/lib/queries";
import { formatDate } from "@/lib/format";
import type { DeploymentOut } from "@/lib/types";
import { QueryState } from "@/components/QueryState";
import { AutomationPausedBadge } from "@/components/AutomationPausedBadge";
import { BenchmarkBadge } from "@/components/BenchmarkBadge";
import { CanaryAnalysis } from "@/components/CanaryAnalysis";
import { MetricsOverTime } from "@/components/MetricsOverTime";
import { DeploymentActions } from "@/components/DeploymentActions";
import { StatusBadge } from "@/components/StatusBadge";
import { Timeline } from "@/components/Timeline";
import { TrafficBar } from "@/components/TrafficBar";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PolicyChecks } from "@/components/PolicyChecks";
import { AppliedPolicy } from "@/components/AppliedPolicy";
import { DecisionPanel } from "@/components/viz/DecisionPanel";
import { TrafficProgression } from "@/components/viz/TrafficProgression";
import { trafficSeries } from "@/lib/derive";
import { Skeleton } from "@/components/ui/skeleton";

const ROUTER_MANAGED_STATUSES = new Set(["CANARY_RUNNING", "EVALUATING"]);

/** Desired (DB) vs observed (router) revision for this deployment's traffic split -
 * see docs/DESIGN_NOTES.md#desired-observed-reconciliation. Only meaningful for the
 * deployment the router is *currently* supposed to serve: a terminal deployment's
 * revision is frozen, and the router only reports the live one, so comparing an
 * old deployment against it would be a false alarm, not real drift. */
function RouterReconciliationStatus({ deployment }: { deployment: DeploymentOut }) {
  const managed = ROUTER_MANAGED_STATUSES.has(deployment.status);
  const { data: router } = useRouterObserved(managed);
  const observed = router ? observedFor(router, deployment.model_name) : undefined;

  if (!deployment.traffic_allocation) return null;
  const desired = deployment.traffic_allocation.revision;
  const base = "flex items-center gap-1.5 text-xs";

  if (!managed) {
    return (
      <p className={`${base} text-muted-foreground`}>
        Desired revision <span className="font-mono">{desired}</span> · final, not currently router-managed
      </p>
    );
  }
  if (!observed) {
    return (
      <p className={`${base} text-muted-foreground`}>
        <Loader2 className="size-3.5 animate-spin" /> Checking router state…
      </p>
    );
  }
  if (!observed.reachable) {
    return (
      <p className={`${base} font-medium text-danger`}>
        <Unplug className="size-3.5" /> Desired revision {desired} — router unreachable, actual traffic
        state unknown.
      </p>
    );
  }
  if (observed.deployment_id !== deployment.id) {
    return (
      <p className={`${base} text-muted-foreground`}>
        Desired revision {desired} · router not yet synced to this deployment
      </p>
    );
  }
  const inSync = observed.revision === desired;
  return (
    <p className={`${base} ${inSync ? "text-muted-foreground" : "font-medium text-warning"}`}>
      {inSync ? <CircleCheck className="size-3.5 text-success" /> : <CircleAlert className="size-3.5" />}
      Desired <span className="font-mono">r{desired}</span> · Observed{" "}
      <span className="font-mono">r{observed.revision}</span>
      {inSync ? " · in sync" : " — router behind, self-corrects on next reconcile tick"}
    </p>
  );
}

function DeploymentDetail({ deployment }: { deployment: DeploymentOut }) {
  const live = isLive(deployment.status);
  const timeline = useTimeline(deployment.id, live);
  const series = trafficSeries(deployment);
  const policyCount = timeline.data?.filter((i) => i.type === "policy_evaluation").length;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-mono text-lg font-semibold tracking-tight">{deployment.model_name}</h1>
            <StatusBadge status={deployment.status} />
            {deployment.is_benchmark ? <BenchmarkBadge /> : null}
            {deployment.automation_paused ? <AutomationPausedBadge /> : null}
          </div>
          <p className="font-mono text-sm text-muted-foreground">
            {deployment.stable_version} <span className="text-muted-foreground/50">→</span>{" "}
            <span className="text-canary">{deployment.canary_version}</span>
            <span className="ml-3 text-xs" title={deployment.id}>
              #{deployment.id.slice(0, 8)} · started {formatDate(deployment.started_at)}
            </span>
          </p>
        </div>
        <DeploymentActions deployment={deployment} />
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>Traffic</CardTitle>
            <CardDescription>Desired split, how it moved over time, and whether the router agrees.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {deployment.traffic_allocation ? (
              <TrafficBar stableVersion={deployment.stable_version} targets={deployment.traffic_allocation.targets} />
            ) : (
              <p className="text-sm text-muted-foreground">No traffic allocation recorded.</p>
            )}
            {series ? (
              <TrafficProgression points={series} />
            ) : (
              <p className="rounded-md border border-dashed px-3 py-6 text-center text-xs text-muted-foreground">
                No traffic advances recorded for this rollout, so there is no progression to plot.
              </p>
            )}
            <RouterReconciliationStatus deployment={deployment} />
          </CardContent>
        </Card>
        <Card>
          <CardContent>
            <DecisionPanel deployment={deployment} showLink={false} />
          </CardContent>
        </Card>
      </div>

      <Tabs defaultValue="analysis" className="gap-4">
        <TabsList>
          <TabsTrigger value="analysis">Canary analysis</TabsTrigger>
          <TabsTrigger value="policies" className="gap-1.5">
            Policy checks
            {policyCount != null ? (
              <span className="rounded bg-foreground/8 px-1 font-mono text-[10px] tabular">{policyCount}</span>
            ) : null}
          </TabsTrigger>
          <TabsTrigger value="timeline" className="gap-1.5">
            Timeline
            {timeline.data ? (
              <span className="rounded bg-foreground/8 px-1 font-mono text-[10px] tabular">{timeline.data.length}</span>
            ) : null}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="analysis">
          <Card>
            <CardContent className="space-y-8">
              <MetricsOverTime deployment={deployment} live={live} />
              <CanaryAnalysis deploymentId={deployment.id} live={live} startedAt={deployment.started_at} />
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="policies">
          <div className="space-y-4">
            <QueryState query={timeline}>{(items) => <PolicyChecks items={items} />}</QueryState>
            <AppliedPolicy config={deployment.policy_config} />
          </div>
        </TabsContent>
        <TabsContent value="timeline">
          <Card>
            <CardContent>
              <QueryState query={timeline}>{(items) => <Timeline items={items} />}</QueryState>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default function DeploymentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const query = useDeployment(id);

  return (
    <div className="space-y-4">
      <QueryState
        query={query}
        skeleton={
          <div className="space-y-4">
            <Skeleton className="h-10 w-72" />
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-72 w-full" />
          </div>
        }
      >
        {(deployment) => <DeploymentDetail deployment={deployment} />}
      </QueryState>
    </div>
  );
}
