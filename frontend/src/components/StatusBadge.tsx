import { cn } from "@/lib/utils";
import { STATUS_META, TONE_DOT, TONE_PILL } from "@/lib/status";
import { LIVE_STATUSES } from "@/lib/queries";
import type { DeploymentStatus } from "@/lib/types";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export function StatusBadge({ status, className }: { status: DeploymentStatus; className?: string }) {
  const meta = STATUS_META[status];
  const live = LIVE_STATUSES.has(status);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "inline-flex h-5 items-center gap-1.5 rounded-full border px-2 text-xs font-medium whitespace-nowrap",
            TONE_PILL[meta.tone],
            className,
          )}
        >
          <span
            aria-hidden
            className={cn("size-1.5 rounded-full", TONE_DOT[meta.tone], live && "live-dot")}
          />
          {meta.label}
        </span>
      </TooltipTrigger>
      <TooltipContent>{meta.description}</TooltipContent>
    </Tooltip>
  );
}
