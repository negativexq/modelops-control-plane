/** Marks a deployment whose automation is on manual hold (DeploymentOut.
 * automation_paused) - the worker will not evaluate, advance, promote, or roll
 * it back on its own until it's resumed. See DeploymentActions for the
 * pause/resume control and backend/docs/DESIGN_NOTES.md#manual-automation-hold
 * for why this exists. */
export function AutomationPausedBadge() {
  return (
    <span
      title="Automation is on manual hold - the worker will not act on this deployment"
      className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium tracking-wide text-amber-800 uppercase dark:bg-amber-950 dark:text-amber-200"
    >
      Automation paused
    </span>
  );
}
