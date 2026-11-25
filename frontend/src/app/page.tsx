"use client";

import Link from "next/link";
import { ArrowRight, GitBranch } from "lucide-react";
import { useDeployments } from "@/lib/queries";
import { formatDate } from "@/lib/format";
import type { DeploymentOut } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { QueryState } from "@/components/QueryState";
import { StatusBadge } from "@/components/StatusBadge";
import { TrafficBar } from "@/components/TrafficBar";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd>{children}</dd>
    </div>
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
            Pick a candidate model version and send it a slice of live traffic. The control loop
            takes it from there.
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

  // Newest first (GET /api/deployments).
  const latest = deployments[0];

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Latest rollout</CardTitle>
          <CardAction>
            <Button asChild variant="ghost" size="xs">
              <Link href={`/deployments/${latest.id}`}>
                Analysis <ArrowRight />
              </Link>
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          <dl className="divide-y text-sm">
            <Row label="Model">
              <span className="font-mono">{latest.model_name}</span>
            </Row>
            <Row label="Stable → canary">
              <span className="font-mono text-xs">
                {latest.stable_version} → <span className="text-canary">{latest.canary_version}</span>
              </span>
            </Row>
            <Row label="Status">
              <StatusBadge status={latest.status} />
            </Row>
            <Row label="Started">
              <span className="font-mono text-xs tabular">{formatDate(latest.started_at)}</span>
            </Row>
            <Row label="Completed">
              <span className="font-mono text-xs tabular">{formatDate(latest.completed_at)}</span>
            </Row>
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Traffic split</CardTitle>
        </CardHeader>
        <CardContent>
          {latest.traffic_allocation ? (
            <TrafficBar stableVersion={latest.stable_version} targets={latest.traffic_allocation.targets} />
          ) : (
            <p className="text-sm text-muted-foreground">No traffic allocation recorded.</p>
          )}
        </CardContent>
      </Card>

      <Card className="md:col-span-2">
        <CardHeader>
          <CardTitle>Recent rollouts</CardTitle>
          <CardAction>
            <Button asChild variant="ghost" size="xs">
              <Link href="/deployments">
                All <ArrowRight />
              </Link>
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          <ul className="divide-y text-sm">
            {deployments.slice(0, 6).map((deployment) => (
              <li key={deployment.id}>
                <Link
                  href={`/deployments/${deployment.id}`}
                  className="-mx-2 flex items-center justify-between gap-3 rounded-md px-2 py-2 transition-colors hover:bg-accent"
                >
                  <span className="truncate font-mono">
                    {deployment.model_name}{" "}
                    <span className="text-xs text-muted-foreground">
                      {deployment.stable_version} → {deployment.canary_version}
                    </span>
                  </span>
                  <StatusBadge status={deployment.status} />
                </Link>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}

export default function OverviewPage() {
  const query = useDeployments();
  return (
    <div className="space-y-6">
      <PageHeader title="Overview" />
      <QueryState query={query}>{(deployments) => <OverviewContent deployments={deployments} />}</QueryState>
    </div>
  );
}
