"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ChevronLeft, CircleAlert, CircleCheck, Loader2, Unplug } from "lucide-react";
import { isLive, useDeployment, useRouterObserved, useTimeline } from "@/lib/queries";
import { formatDate } from "@/lib/format";
import type { DeploymentOut } from "@/lib/types";
import { QueryState } from "@/components/QueryState";
import { AutomationPausedBadge } from "@/components/AutomationPausedBadge";
import { BenchmarkBadge } from "@/components/BenchmarkBadge";
import { CanaryAnalysis } from "@/components/CanaryAnalysis";
import { DeploymentActions } from "@/components/DeploymentActions";
import { StatusBadge } from "@/components/StatusBadge";
import { Timeline } from "@/components/Timeline";
import { TrafficBar } from "@/components/TrafficBar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

const ROUTER_MANAGED_STATUSES = new Set(["CANARY_RUNNING", "EVALUATING"]);

/** Desired (DB) vs observed (router) revision for this deployment's traffic split -
 * see docs/DESIGN_NOTES.md#desired-observed-reconciliation. Only meaningful for the
 * deployment the router is *currently* supposed to serve: a terminal deployment's
 * revision is frozen, and the router only reports the live one, so comparing an
 * old deployment against it would be a false alarm, not real drift. */
function RouterReconciliationStatus({ deployment }: { deployment: DeploymentOut }) {
  const managed = ROUTER_MANAGED_STATUSES.has(deployment.status);
  const { data: observed } = useRouterObserved(managed);

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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  );
}

function DeploymentDetail({ deployment }: { deployment: DeploymentOut }) {
  const live = isLive(deployment.status);
  const timeline = useTimeline(deployment.id, live);

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
          </p>
        </div>
        <DeploymentActions deployment={deployment} />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Traffic split</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {deployment.traffic_allocation ? (
              <>
                <TrafficBar
                  stableVersion={deployment.stable_version}
                  targets={deployment.traffic_allocation.targets}
                />
                <RouterReconciliationStatus deployment={deployment} />
              </>
            ) : (
              <p className="text-sm text-muted-foreground">No traffic allocation recorded.</p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardContent>
            <dl className="grid grid-cols-2 gap-4">
              <Field label="Created">
                <span className="font-mono text-xs tabular">{formatDate(deployment.created_at)}</span>
              </Field>
              <Field label="Started">
                <span className="font-mono text-xs tabular">{formatDate(deployment.started_at)}</span>
              </Field>
              <Field label="Completed">
                <span className="font-mono text-xs tabular">{formatDate(deployment.completed_at)}</span>
              </Field>
              <Field label="ID">
                <span className="block truncate font-mono text-xs" title={deployment.id}>
                  {deployment.id.slice(0, 8)}
                </span>
              </Field>
            </dl>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent>
          <CanaryAnalysis deploymentId={deployment.id} live={live} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Timeline</CardTitle>
        </CardHeader>
        <CardContent>
          <QueryState query={timeline}>{(items) => <Timeline items={items} />}</QueryState>
        </CardContent>
      </Card>
    </div>
  );
}

export default function DeploymentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const query = useDeployment(id);

  return (
    <div className="space-y-4">
      <Link
        href="/deployments"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ChevronLeft className="size-4" /> Deployments
      </Link>
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
