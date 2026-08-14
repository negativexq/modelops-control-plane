"use client";

import Link from "next/link";
import { listDeployments } from "@/lib/api";
import { useAsync } from "@/lib/useAsync";
import { AsyncBoundary } from "@/components/AsyncBoundary";
import { AutomationPausedBadge } from "@/components/AutomationPausedBadge";
import { BenchmarkBadge } from "@/components/BenchmarkBadge";
import { Card } from "@/components/Card";
import { DeploymentActions } from "@/components/DeploymentActions";
import { StatusBadge } from "@/components/StatusBadge";
import { formatDate } from "@/lib/format";

export default function DeploymentsPage() {
  const { data, error, loading, refetch } = useAsync(() => listDeployments());

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">Deployments</h1>

      <AsyncBoundary data={data} error={error} loading={loading} onRetry={refetch}>
        {(deployments) =>
          deployments.length === 0 ? (
            <Card title="No deployments yet">
              <p className="text-sm text-zinc-600 dark:text-zinc-400">
                Start one from the{" "}
                <Link href="/models" className="underline underline-offset-2">
                  Models
                </Link>{" "}
                page.
              </p>
            </Card>
          ) : (
            <Card>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[820px] text-left text-sm">
                  <thead>
                    <tr className="border-b border-zinc-200 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
                      <th className="py-2 pr-4">Model</th>
                      <th className="py-2 pr-4">Stable</th>
                      <th className="py-2 pr-4">Canary</th>
                      <th className="py-2 pr-4">Status</th>
                      <th className="py-2 pr-4">Started</th>
                      <th className="py-2 pr-4">Completed</th>
                      <th className="py-2 pr-4 text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-100 dark:divide-zinc-900">
                    {deployments.map((deployment) => (
                      <tr key={deployment.id}>
                        <td className="py-3 pr-4">
                          <div className="flex items-center gap-2">
                            <Link
                              href={`/deployments/${deployment.id}`}
                              className="font-medium text-blue-600 underline-offset-2 hover:underline dark:text-blue-400"
                            >
                              {deployment.model_name}
                            </Link>
                            {deployment.is_benchmark ? <BenchmarkBadge /> : null}
                            {deployment.automation_paused ? <AutomationPausedBadge /> : null}
                          </div>
                        </td>
                        <td className="py-3 pr-4">{deployment.stable_version}</td>
                        <td className="py-3 pr-4">{deployment.canary_version}</td>
                        <td className="py-3 pr-4">
                          <StatusBadge status={deployment.status} />
                        </td>
                        <td className="py-3 pr-4 whitespace-nowrap text-zinc-600 dark:text-zinc-400">
                          {formatDate(deployment.started_at)}
                        </td>
                        <td className="py-3 pr-4 whitespace-nowrap text-zinc-600 dark:text-zinc-400">
                          {formatDate(deployment.completed_at)}
                        </td>
                        <td className="py-3 pr-4">
                          <DeploymentActions
                            deployment={deployment}
                            onChanged={refetch}
                            size="sm"
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )
        }
      </AsyncBoundary>
    </div>
  );
}
