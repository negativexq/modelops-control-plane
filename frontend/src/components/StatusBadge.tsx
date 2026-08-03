import type { DeploymentStatus } from "@/lib/types";

const STYLES: Record<DeploymentStatus, string> = {
  PENDING: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  DEPLOYING: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  CANARY_RUNNING: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
  EVALUATING: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  PROMOTING: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  PROMOTED: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  ROLLING_BACK: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  ROLLED_BACK: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  FAILED: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  INCONCLUSIVE: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
};

export function StatusBadge({ status }: { status: DeploymentStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STYLES[status]}`}
    >
      {status.replaceAll("_", " ")}
    </span>
  );
}
