import { PauseCircle } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** Manual automation hold (DeploymentOut.automation_paused): the worker will not
 * evaluate, advance, promote or roll back on its own until resumed. See
 * docs/DESIGN_NOTES.md#manual-automation-hold. */
export function AutomationPausedBadge() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex h-5 items-center gap-1 rounded-full border border-warning/30 bg-warning/10 px-1.5 text-[10px] font-medium tracking-wide text-warning uppercase">
          <PauseCircle className="size-3" aria-hidden />
          Paused
        </span>
      </TooltipTrigger>
      <TooltipContent>Automation on manual hold. The worker will not act on this deployment.</TooltipContent>
    </Tooltip>
  );
}
