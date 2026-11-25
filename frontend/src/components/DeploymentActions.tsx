"use client";

import { useState } from "react";
import { ArrowUpRight, Loader2, Pause, Play, TriangleAlert, Undo2 } from "lucide-react";
import { useDeploymentAction } from "@/lib/queries";
import type { DeploymentOut, DeploymentStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const ACTIONABLE_STATUSES: DeploymentStatus[] = ["CANARY_RUNNING", "EVALUATING", "INCONCLUSIVE"];

// Mirrors the worker's own ACTIVE_STATUSES (app/worker/loop.py) - the automated
// worker sweeps and acts on every deployment in one of these statuses on every
// poll cycle, unconditionally (unless automation_paused). INCONCLUSIVE is
// deliberately excluded: once a deployment freezes there, the worker has already
// stopped touching it (docs/DESIGN_NOTES.md#automated-promotion--rollback), so a
// manual action there can't race it.
const WORKER_MANAGED_STATUSES: DeploymentStatus[] = ["CANARY_RUNNING", "EVALUATING"];

// Mirrors backend/app/control_plane/models.py's TERMINAL_STATUSES - pause/resume
// are rejected (409) once a deployment is done, same guard the backend applies.
const TERMINAL_STATUSES: DeploymentStatus[] = ["PROMOTED", "ROLLED_BACK", "FAILED"];

type Confirm = "promote" | "rollback" | null;

/**
 * Promote / rollback / pause-resume, shared by the list and detail pages.
 * Promote and rollback move 100% of live traffic, so both go through a confirm
 * dialog that spells out exactly what will happen. Nothing is updated
 * optimistically: the query cache is invalidated after the control plane answers.
 */
export function DeploymentActions({
  deployment,
  size = "md",
}: {
  deployment: DeploymentOut;
  size?: "sm" | "md";
}) {
  const promote = useDeploymentAction("promote");
  const rollback = useDeploymentAction("rollback");
  const pause = useDeploymentAction("pause");
  const resume = useDeploymentAction("resume");
  const [confirm, setConfirm] = useState<Confirm>(null);

  const canAct = ACTIONABLE_STATUSES.includes(deployment.status);
  const canPauseOrResume = !TERMINAL_STATUSES.includes(deployment.status);
  // Only actually racing the worker while it's in a status the worker sweeps AND
  // not on hold - a paused CANARY_RUNNING deployment is left alone by the worker.
  const workerManaged =
    WORKER_MANAGED_STATUSES.includes(deployment.status) && !deployment.automation_paused;
  const busy = promote.isPending || rollback.isPending || pause.isPending || resume.isPending;
  const buttonSize = size === "sm" ? "xs" : "sm";

  if (!canAct && !canPauseOrResume) return null;

  return (
    <div className={cn("flex items-center gap-1.5", size === "md" && "flex-wrap justify-end")}>
      {workerManaged && size === "md" ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="mr-1 inline-flex items-center gap-1 text-xs text-warning">
              <TriangleAlert className="size-3.5" aria-hidden />
              Automated rollout in progress
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">
            The worker may promote, roll back or advance traffic on its own. Pause automation
            before acting manually to avoid racing it.
          </TooltipContent>
        </Tooltip>
      ) : null}

      {canPauseOrResume ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="outline"
              size={buttonSize}
              disabled={busy}
              onClick={() =>
                deployment.automation_paused
                  ? resume.mutate(deployment.id)
                  : pause.mutate(deployment.id)
              }
              aria-label={deployment.automation_paused ? "Resume automation" : "Pause automation"}
            >
              {pause.isPending || resume.isPending ? (
                <Loader2 className="animate-spin" />
              ) : deployment.automation_paused ? (
                <Play />
              ) : (
                <Pause />
              )}
              {size === "md" ? (deployment.automation_paused ? "Resume" : "Pause") : null}
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            {deployment.automation_paused
              ? "Let the worker act on this deployment again"
              : "Stop the worker from acting on this deployment"}
          </TooltipContent>
        </Tooltip>
      ) : null}

      {canAct ? (
        <>
          <Button
            size={buttonSize}
            variant="outline"
            disabled={busy}
            onClick={() => setConfirm("rollback")}
            className="text-danger hover:bg-danger/10 hover:text-danger"
          >
            {rollback.isPending ? <Loader2 className="animate-spin" /> : <Undo2 />}
            Rollback
          </Button>
          <Button
            size={buttonSize}
            disabled={busy}
            onClick={() => setConfirm("promote")}
            className="bg-success text-background hover:bg-success/85"
          >
            {promote.isPending ? <Loader2 className="animate-spin" /> : <ArrowUpRight />}
            Promote
          </Button>
        </>
      ) : null}

      <AlertDialog open={confirm !== null} onOpenChange={(open) => !open && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirm === "promote" ? "Promote canary to 100%?" : "Roll back to stable?"}
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>
                  {confirm === "promote" ? (
                    <>
                      <span className="font-mono text-foreground">{deployment.canary_version}</span>{" "}
                      will serve all live traffic for{" "}
                      <span className="font-mono text-foreground">{deployment.model_name}</span>.
                    </>
                  ) : (
                    <>
                      All live traffic for{" "}
                      <span className="font-mono text-foreground">{deployment.model_name}</span>{" "}
                      returns to{" "}
                      <span className="font-mono text-foreground">{deployment.stable_version}</span>.
                    </>
                  )}{" "}
                  This ends the rollout and can&apos;t be undone from here.
                </p>
                {workerManaged ? (
                  <p className="text-warning">
                    The worker is still managing this rollout and may act at the same moment.
                  </p>
                ) : null}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className={
                confirm === "promote"
                  ? "bg-success text-background hover:bg-success/85"
                  : "bg-danger text-background hover:bg-danger/85"
              }
              onClick={() => {
                if (confirm === "promote") promote.mutate(deployment.id);
                if (confirm === "rollback") rollback.mutate(deployment.id);
              }}
            >
              {confirm === "promote" ? "Promote" : "Roll back"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
