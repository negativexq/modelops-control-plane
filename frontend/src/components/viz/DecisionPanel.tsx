"use client";

import Link from "next/link";
import { ArrowRight, ArrowUpRight, CircleHelp, ShieldX, Undo2 } from "lucide-react";
import { useTimeline, isLive } from "@/lib/queries";
import { decisionOf, durationSeconds, formatDuration, latestPolicyResults, relativeTime } from "@/lib/derive";
import { STATUS_META, TONE_TEXT } from "@/lib/status";
import type { DeploymentOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PolicyResultBadge } from "@/components/PolicyResultBadge";
import { ActorBadge } from "./ActorBadge";

const OUTCOME_ICON = {
  PROMOTED: ArrowUpRight,
  ROLLED_BACK: Undo2,
  INCONCLUSIVE: CircleHelp,
  FAILED: ShieldX,
} as const;

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="truncate">{children}</span>
    </div>
  );
}

/**
 * Inspector for one rollout's decision (after ObsidianUI's PR side panel): what
 * was decided, by whom, how long it took, and the policy evidence behind it.
 * The evidence is the latest result per policy from the timeline, failures first.
 */
export function DecisionPanel({ deployment, showLink = true }: { deployment: DeploymentOut; showLink?: boolean }) {
  const decision = decisionOf(deployment);
  const meta = STATUS_META[deployment.status];
  const timeline = useTimeline(deployment.id, isLive(deployment.status));
  const evidence = timeline.data ? latestPolicyResults(timeline.data) : null;
  const Icon = decision ? OUTCOME_ICON[decision.outcome] : null;

  return (
    <div className="flex h-full flex-col gap-4">
      <div className="space-y-1.5">
        <p className={cn("flex items-center gap-1.5 text-xs font-medium", TONE_TEXT[meta.tone])}>
          {Icon ? <Icon className="size-3.5" /> : <span className="live-dot size-1.5 rounded-full bg-canary text-canary" />}
          {decision ? `${meta.label} ${relativeTime(decision.at)}` : `${meta.label} · in progress`}
        </p>
        <p className="font-mono text-base leading-snug font-semibold">{deployment.model_name}</p>
        <p className="font-mono text-xs text-muted-foreground">
          {deployment.stable_version} → <span className="text-canary">{deployment.canary_version}</span>
        </p>
      </div>

      <div className="divide-y border-y">
        <Meta label="Decided by">{decision ? <ActorBadge actor={decision.actor} /> : "—"}</Meta>
        <Meta label="Duration">
          <span className="font-mono text-xs tabular">{formatDuration(durationSeconds(deployment))}</span>
        </Meta>
      </div>

      <div className="min-h-0 flex-1 space-y-2">
        <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Why</p>
        {evidence == null ? (
          <div className="space-y-2">
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
          </div>
        ) : evidence.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No policy checks recorded{decision?.actor === "manual" ? ": this was an operator decision." : " yet."}
          </p>
        ) : (
          <ul className="space-y-2">
            {evidence.slice(0, 4).map((item) => (
              <li key={item.id} className="rounded-md border bg-muted/30 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-xs">{item.policy_name}</span>
                  <PolicyResultBadge result={item.result} />
                </div>
                <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{item.explanation}</p>
              </li>
            ))}
          </ul>
        )}
      </div>

      {showLink ? (
        <Button asChild variant="outline" size="sm" className="w-full">
          <Link href={`/deployments/${deployment.id}`}>
            Open rollout <ArrowRight />
          </Link>
        </Button>
      ) : null}
    </div>
  );
}
