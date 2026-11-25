"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { GitBranch } from "lucide-react";
import { useDeployments } from "@/lib/queries";
import { formatDate } from "@/lib/format";
import { PageHeader } from "@/components/PageHeader";
import { QueryState } from "@/components/QueryState";
import { AutomationPausedBadge } from "@/components/AutomationPausedBadge";
import { BenchmarkBadge } from "@/components/BenchmarkBadge";
import { DeploymentActions } from "@/components/DeploymentActions";
import { StatusBadge } from "@/components/StatusBadge";
import { TrafficBar } from "@/components/TrafficBar";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export default function DeploymentsPage() {
  const router = useRouter();
  const query = useDeployments();

  return (
    <div className="space-y-6">
      <PageHeader title="Deployments" description="Every canary rollout, newest first." />

      <QueryState query={query}>
        {(deployments) =>
          deployments.length === 0 ? (
            <Empty className="border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <GitBranch />
                </EmptyMedia>
                <EmptyTitle>No deployments yet</EmptyTitle>
                <EmptyDescription>Start a canary rollout to see it tracked here.</EmptyDescription>
              </EmptyHeader>
              <EmptyContent>
                <Button asChild size="sm">
                  <Link href="/models">Start a rollout</Link>
                </Button>
              </EmptyContent>
            </Empty>
          ) : (
            <Card className="py-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-4">Model</TableHead>
                    <TableHead>Versions</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="w-40">Traffic</TableHead>
                    <TableHead>Started</TableHead>
                    <TableHead className="pr-4 text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {deployments.map((deployment) => (
                    <TableRow
                      key={deployment.id}
                      className="cursor-pointer"
                      onClick={() => router.push(`/deployments/${deployment.id}`)}
                    >
                      <TableCell className="pl-4">
                        <div className="flex items-center gap-2">
                          <Link
                            href={`/deployments/${deployment.id}`}
                            className="font-mono font-medium hover:underline"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {deployment.model_name}
                          </Link>
                          {deployment.is_benchmark ? <BenchmarkBadge /> : null}
                          {deployment.automation_paused ? <AutomationPausedBadge /> : null}
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">
                        {deployment.stable_version} <span className="text-muted-foreground/50">→</span>{" "}
                        <span className="text-foreground">{deployment.canary_version}</span>
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={deployment.status} />
                      </TableCell>
                      <TableCell>
                        {deployment.traffic_allocation ? (
                          <TrafficBar
                            size="sm"
                            showLegend={false}
                            stableVersion={deployment.stable_version}
                            targets={deployment.traffic_allocation.targets}
                          />
                        ) : null}
                      </TableCell>
                      <TableCell className="font-mono text-xs whitespace-nowrap text-muted-foreground tabular">
                        {formatDate(deployment.started_at)}
                      </TableCell>
                      <TableCell className="pr-4" onClick={(e) => e.stopPropagation()}>
                        <div className="flex justify-end">
                          <DeploymentActions deployment={deployment} size="sm" />
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Card>
          )
        }
      </QueryState>
    </div>
  );
}
