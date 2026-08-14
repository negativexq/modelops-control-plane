"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { getDeployment, getDeploymentTimeline } from "@/lib/api";
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
          <TrafficBar targets={deployment.traffic_allocation.targets} />
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
