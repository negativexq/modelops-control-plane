"use client";

import Link from "next/link";
import { Activity, ArrowUpRight, Hourglass, ShieldAlert, Unplug, Zap } from "lucide-react";
import { isLive, useDeployments, useGatewayView } from "@/lib/queries";
import type { DeploymentOut, GatewayModel, GatewayView } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ControlLoop } from "@/components/viz/ControlLoop";
import { GatewayFlow, GatewayLegend } from "@/components/viz/GatewayFlow";
import { KpiCard } from "@/components/viz/KpiCard";

function deploymentFor(model: GatewayModel, deployments: DeploymentOut[] | undefined) {
  return deployments?.find((d) => d.id === model.deployment_id);
}

function Kpis({ view }: { view: GatewayView }) {
  const versions = view.models.flatMap((m) => m.versions);
  const rps = versions.reduce((s, v) => s + v.requests_per_second, 0);
  const inFlight = versions.reduce((s, v) => s + v.in_flight, 0);
  const failedFast = versions.reduce((s, v) => s + v.outcomes.gateway_error + v.outcomes.rejected, 0);
  // Breakers are per version, and a version can serve several models.
  const open = [...new Map(versions.filter((v) => v.breaker !== "closed").map((v) => [v.version, v])).values()];
  const limited = view.models.reduce((s, m) => s + m.rate_limited_total, 0);
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <KpiCard label="Requests / s" icon={Activity} tone="text-canary" value={rps} decimals={1} emphasis={rps > 0} hint="Across every model, last 10 seconds" />
      <KpiCard label="In flight" icon={Hourglass} value={inFlight} hint="Forwarded, waiting on a model" />
      <KpiCard
        label="Unanswered"
        icon={Unplug}
        tone={failedFast ? "text-danger" : undefined}
        value={failedFast}
        hint={`Unreachable, not ready or failed fast, last 60s${limited ? ` · ${limited} rate-limited` : ""}`}
      />
      <KpiCard
        label="Breakers not closed"
        icon={ShieldAlert}
        tone={open.length ? "text-danger" : "text-success"}
        value={open.length}
        hint={open.length ? open.map((v) => `${v.version} ${v.breaker.replace("_", "-")}`).join(", ") : "Every version is taking traffic"}
      />
    </div>
  );
}

function ModelCard({ model, view, deployment }: { model: GatewayModel; view: GatewayView; deployment: DeploymentOut | undefined }) {
  const live = isLive(deployment?.status);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 font-mono text-sm">
          {model.model_name}
          {deployment ? <StatusBadge status={deployment.status} /> : null}
        </CardTitle>
        <CardDescription>
          {deployment ? (
            <>
              {deployment.stable_version} → {deployment.canary_version}
            </>
          ) : (
            "No rollout on record for this split"
          )}
        </CardDescription>
        {deployment ? (
          <CardAction>
            <Link href={`/deployments/${deployment.id}`} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
              Rollout <ArrowUpRight className="size-3" />
            </Link>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-5">
        <GatewayFlow model={model} policy={view.policy!} deployment={deployment} />
        {deployment && live ? <ControlLoop deployment={deployment} model={model} /> : null}
      </CardContent>
    </Card>
  );
}

export default function GatewayPage() {
  const gateway = useGatewayView();
  const { data: deployments } = useDeployments();
  const view = gateway.data;

  const models = view?.models ?? [];
  const active = models.filter(
    (m) => m.versions.some((v) => v.window_requests > 0 || v.in_flight > 0) || isLive(deploymentFor(m, deployments)?.status),
  );
  const idle = models.filter((m) => !active.includes(m));

  return (
    <div className="space-y-6">
      <PageHeader
        title="Gateway"
        description="Every prediction's path through the router, live: rate limit, sticky routing and a circuit breaker per version, then the control loop that moves the split."
        actions={<GatewayLegend />}
      />

      {gateway.isPending ? (
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-[142px]" />
            ))}
          </div>
          <Skeleton className="h-64" />
        </div>
      ) : gateway.isError ? (
        <p className="text-sm text-danger">{gateway.error.message}</p>
      ) : view && !view.reachable ? (
        <div className="flex h-48 flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-danger/40 text-sm">
          <Unplug className="size-5 text-danger" />
          <p className="font-medium text-danger">Router unreachable</p>
          <p className="text-xs text-muted-foreground">No live traffic view until it answers again.</p>
        </div>
      ) : view ? (
        <>
          <Kpis view={view} />
          {active.length === 0 ? (
            <div className="flex h-40 flex-col items-center justify-center gap-1 rounded-xl border border-dashed text-sm text-muted-foreground">
              <Zap className="size-5" />
              <p>No traffic in the last minute.</p>
              <p className="text-xs">Start a rollout or run a benchmark and the flow lights up here.</p>
            </div>
          ) : (
            <div className="space-y-4">
              {active.map((m) => (
                <ModelCard key={m.model_name} model={m} view={view} deployment={deploymentFor(m, deployments)} />
              ))}
            </div>
          )}
          {idle.length ? (
            <section className="space-y-2">
              <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Idle splits</h2>
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                {idle.map((m) => (
                  <div key={m.model_name} className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-xs">
                    <span className="truncate font-mono">{m.model_name}</span>
                    <span className="font-mono text-muted-foreground tabular">
                      {m.versions.map((v) => `${v.version} ${(v.weight * 100).toFixed(0)}%`).join(" · ")}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
