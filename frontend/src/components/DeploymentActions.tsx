"use client";

import { promoteDeployment, rollbackDeployment } from "@/lib/api";
import { useMutation } from "@/lib/useMutation";
import type { DeploymentOut, DeploymentStatus } from "@/lib/types";

const ACTIONABLE_STATUSES: DeploymentStatus[] = ["CANARY_RUNNING", "EVALUATING", "INCONCLUSIVE"];

interface DeploymentActionsProps {
  deployment: DeploymentOut;
  onChanged: () => void;
  size?: "sm" | "md";
}

/**
 * Promote/rollback buttons shared by the deployments list and detail pages. Always
 * calls the real API and then `onChanged()` (the caller's refetch) - never mutates
 * local state directly, so what's on screen always matches what the control plane
 * actually persisted.
 */
export function DeploymentActions({ deployment, onChanged, size = "md" }: DeploymentActionsProps) {
  const promote = useMutation(() => promoteDeployment(deployment.id), "Promoted to 100% canary.");
  const rollback = useMutation(
    () => rollbackDeployment(deployment.id),
    "Rolled back to 100% stable.",
  );

  const canAct = ACTIONABLE_STATUSES.includes(deployment.status);
  const busy = promote.submitting || rollback.submitting;
  const padding = size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm";

  async function handlePromote() {
    const result = await promote.run();
    if (result) onChanged();
  }

  async function handleRollback() {
    const result = await rollback.run();
    if (result) onChanged();
  }

  return (
    <div className="flex flex-col items-end gap-1.5">
      <div className="flex gap-2">
        <button
          type="button"
          onClick={handlePromote}
          disabled={!canAct || busy}
          title={canAct ? "Promote canary to 100% traffic" : "Only available while running/evaluating"}
          className={`rounded-md bg-emerald-600 font-medium text-white disabled:cursor-not-allowed disabled:opacity-40 ${padding}`}
        >
          {promote.submitting ? "Promoting…" : "Promote"}
        </button>
        <button
          type="button"
          onClick={handleRollback}
          disabled={!canAct || busy}
          title={canAct ? "Roll back all traffic to stable" : "Only available while running/evaluating"}
          className={`rounded-md bg-red-600 font-medium text-white disabled:cursor-not-allowed disabled:opacity-40 ${padding}`}
        >
          {rollback.submitting ? "Rolling back…" : "Rollback"}
        </button>
      </div>
      {promote.result ? (
        <p
          className={`text-xs ${
            promote.result.kind === "success"
              ? "text-emerald-700 dark:text-emerald-400"
              : "text-red-600 dark:text-red-400"
          }`}
        >
          {promote.result.message}
        </p>
      ) : null}
      {rollback.result ? (
        <p
          className={`text-xs ${
            rollback.result.kind === "success"
              ? "text-emerald-700 dark:text-emerald-400"
              : "text-red-600 dark:text-red-400"
          }`}
        >
          {rollback.result.message}
        </p>
      ) : null}
    </div>
  );
}
