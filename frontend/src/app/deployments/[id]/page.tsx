"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { getDeployment, getDeploymentTimeline, getObservedRouterState } from "@/lib/api";
import { useAsync } from "@/lib/useAsync";
import { AsyncBoundary } from "@/components/AsyncBoundary";
import { AutomationPausedBadge } from "@/components/AutomationPausedBadge";
import { BenchmarkBadge } from "@/components/BenchmarkBadge";
import { Card } from "@/components/Card";
import { CanaryAnalysis } from "@/components/CanaryAnalysis";
import { DeploymentActions } from "@/components/DeploymentActions";
import { StatusBadge } from "@/components/StatusBadge";
import { Timeline } from "@/components/Timeline";
import { TrafficBar } from "@/components/TrafficBar";
import { formatDate } from "@/lib/format";
import type { DeploymentOut } from "@/lib/types";

const ROUTER_MANAGED_STATUSES = new Set(["CANARY_RUNNING", "EVALUATING"]);

/** Desired (DB) vs. observed (router) revision for this deployment's traffic
 * split - see backend/docs/DESIGN_NOTES.md#desired-observed-reconciliation.
 * Only meaningful for the deployment the router is *currently* supposed to be
 * serving - a terminal/historical deployment's revision is frozen, and the
 * router only ever reports state for whichever deployment is live right now,
 * so comparing an old deployment against it would just be a false alarm, not
 * real drift. */
function RouterReconciliationStatus({ deployment }: { deployment: DeploymentOut }) {
  const { data: observed, loading } = useAsync(() => getObservedRouterState(), [deployment.id]);

  if (!deployment.traffic_allocation) return null;
  const desiredRevision = deployment.traffic_allocation.revision;

  if (!ROUTER_MANAGED_STATUSES.has(deployment.status)) {
    return (
      <p className="text-xs text-zinc-500 dark:text-zinc-400">
        Desired revision {desiredRevision} (final - not currently router-managed).
      </p>
    );
  }

  if (loading || !observed) {
    return <p className="text-xs text-zinc-500 dark:text-zinc-400">Checking router state…</p>;
  }

  if (!observed.reachable) {
    return (
      <p className="text-xs font-medium text-red-700 dark:text-red-400">
        Desired revision {desiredRevision} — router unreachable, actual traffic state unknown.
      </p>
    );
  }

  if (observed.deployment_id !== deployment.id) {
    return (
      <p className="text-xs text-zinc-500 dark:text-zinc-400">
        Desired revision {desiredRevision} (router not yet synced to this deployment).
      </p>
    );
  }

  const inSync = observed.revision === desiredRevision;
  return (
    <p
      className={`text-xs ${
        inSync
          ? "text-zinc-500 dark:text-zinc-400"
          : "font-medium text-amber-700 dark:text-amber-400"
      }`}
    >
      Desired revision {desiredRevision} · Observed revision {observed.revision}
      {inSync ? "" : " — router has not caught up yet (will self-correct on the next reconcile tick)"}
    </p>
  );
}

function DeploymentDetailContent({
  deployment,
  onChanged,
}: {
  deployment: DeploymentOut;
  onChanged: () => void;
}) {
  const { data: timeline, error: timelineError, loading: timelineLoading, refetch: refetchTimeline } =
    useAsync(() => getDeploymentTimeline(deployment.id), [deployment.id]);

  return (
    <div className="space-y-6">
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <dl className="grid grid-cols-2 gap-x-8 gap-y-3 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-zinc-500 dark:text-zinc-400">Model</dt>
              <dd className="flex items-center gap-2 font-medium">
                {deployment.model_name}
                {deployment.is_benchmark ? <BenchmarkBadge /> : null}
                {deployment.automation_paused ? <AutomationPausedBadge /> : null}
              </dd>
            </div>
            <div>
              <dt className="text-zinc-500 dark:text-zinc-400">Stable</dt>
              <dd className="font-medium">{deployment.stable_version}</dd>
            </div>
            <div>
              <dt className="text-zinc-500 dark:text-zinc-400">Canary</dt>
              <dd className="font-medium">{deployment.canary_version}</dd>
            </div>
            <div>
              <dt className="text-zinc-500 dark:text-zinc-400">Status</dt>
              <dd>
                <StatusBadge status={deployment.status} />
              </dd>
            </div>
            <div>
              <dt className="text-zinc-500 dark:text-zinc-400">Created</dt>
              <dd>{formatDate(deployment.created_at)}</dd>
            </div>
            <div>
              <dt className="text-zinc-500 dark:text-zinc-400">Started</dt>
              <dd>{formatDate(deployment.started_at)}</dd>
            </div>
            <div>
              <dt className="text-zinc-500 dark:text-zinc-400">Completed</dt>
              <dd>{formatDate(deployment.completed_at)}</dd>
            </div>
          </dl>
          <DeploymentActions deployment={deployment} onChanged={onChanged} />
        </div>
      </Card>

      <Card title="Traffic distribution">
        {deployment.traffic_allocation ? (
          <div className="space-y-2">
            <TrafficBar targets={deployment.traffic_allocation.targets} />
            <RouterReconciliationStatus deployment={deployment} />
          </div>
        ) : (
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            No traffic allocation recorded.
          </p>
        )}
      </Card>

      <Card>
        <CanaryAnalysis deploymentId={deployment.id} />
      </Card>

      <Card title="Timeline">
        <AsyncBoundary
          data={timeline}
          error={timelineError}
          loading={timelineLoading}
          onRetry={refetchTimeline}
        >
          {(items) => <Timeline items={items} />}
        </AsyncBoundary>
      </Card>
    </div>
  );
}

export default function DeploymentDetailPage() {
  const params = useParams<{ id: string }>();
  const deploymentId = params.id;
  const { data, error, loading, refetch } = useAsync(
    () => getDeployment(deploymentId),
    [deploymentId],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link
          href="/deployments"
          className="text-sm text-zinc-500 hover:underline dark:text-zinc-400"
        >
          ← Deployments
        </Link>
      </div>
      <h1 className="text-xl font-semibold tracking-tight">Deployment detail</h1>

      <AsyncBoundary data={data} error={error} loading={loading} onRetry={refetch}>
        {(deployment) => (
          <DeploymentDetailContent deployment={deployment} onChanged={refetch} />
        )}
      </AsyncBoundary>
    </div>
  );
}
