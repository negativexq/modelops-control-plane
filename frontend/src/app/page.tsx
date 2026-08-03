"use client";

import Link from "next/link";
import { listDeployments } from "@/lib/api";
import { useAsync } from "@/lib/useAsync";
import { AsyncBoundary } from "@/components/AsyncBoundary";
import { Card } from "@/components/Card";
import { StatusBadge } from "@/components/StatusBadge";
import { TrafficBar } from "@/components/TrafficBar";
import { formatDate } from "@/lib/format";
import type { DeploymentOut } from "@/lib/types";

function OverviewContent({ deployments }: { deployments: DeploymentOut[] }) {
  if (deployments.length === 0) {
    return (
      <Card title="No deployments yet">
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          Start your first canary rollout from the{" "}
          <Link href="/models" className="underline underline-offset-2">
            Models
          </Link>{" "}
          page.
        </p>
      </Card>
    );
  }

  // Newest first (see GET /api/deployments) - the most recent one is what "active
  // model" means for this overview.
  const latest = deployments[0];

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <Card title="Active model">
        <dl className="space-y-3 text-sm">
          <div className="flex justify-between">
            <dt className="text-zinc-500 dark:text-zinc-400">Model</dt>
            <dd className="font-medium">{latest.model_name}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-zinc-500 dark:text-zinc-400">Stable version</dt>
            <dd className="font-medium">{latest.stable_version}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-zinc-500 dark:text-zinc-400">Canary version</dt>
            <dd className="font-medium">{latest.canary_version}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-zinc-500 dark:text-zinc-400">Deployment status</dt>
            <dd>
              <StatusBadge status={latest.status} />
            </dd>
          </div>
        </dl>
      </Card>

      <Card title="Traffic distribution">
        {latest.traffic_allocation ? (
          <TrafficBar targets={latest.traffic_allocation.targets} />
        ) : (
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            No traffic allocation recorded for this deployment.
          </p>
        )}
      </Card>

      <Card title="Last deployment result">
        <dl className="space-y-3 text-sm">
          <div className="flex justify-between">
            <dt className="text-zinc-500 dark:text-zinc-400">Started</dt>
            <dd>{formatDate(latest.started_at)}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-zinc-500 dark:text-zinc-400">Completed</dt>
            <dd>{formatDate(latest.completed_at)}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-zinc-500 dark:text-zinc-400">Result</dt>
            <dd>
              <StatusBadge status={latest.status} />
            </dd>
          </div>
        </dl>
        <Link
          href={`/deployments/${latest.id}`}
          className="mt-4 inline-block text-sm font-medium text-blue-600 underline-offset-2 hover:underline dark:text-blue-400"
        >
          View canary analysis →
        </Link>
      </Card>

      <Card title="Recent deployments">
        <ul className="divide-y divide-zinc-100 text-sm dark:divide-zinc-900">
          {deployments.slice(0, 5).map((deployment) => (
            <li key={deployment.id} className="flex items-center justify-between gap-3 py-2">
              <Link
                href={`/deployments/${deployment.id}`}
                className="truncate font-medium text-blue-600 underline-offset-2 hover:underline dark:text-blue-400"
              >
                {deployment.model_name}: {deployment.stable_version} vs{" "}
                {deployment.canary_version}
              </Link>
              <StatusBadge status={deployment.status} />
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

export default function OverviewPage() {
  const { data, error, loading, refetch } = useAsync(() => listDeployments());

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">Overview</h1>
      <AsyncBoundary data={data} error={error} loading={loading} onRetry={refetch}>
        {(deployments) => <OverviewContent deployments={deployments} />}
      </AsyncBoundary>
    </div>
  );
}
