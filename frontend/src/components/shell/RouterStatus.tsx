"use client";

import { Radio, Unplug } from "lucide-react";
import { useRouterObserved } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { SidebarMenuButton } from "@/components/ui/sidebar";

/** Router health from GET /api/router/observed. `reachable` is checked first: an
 * unreachable router and a reachable-but-unconfigured one both report
 * deployment_id: null. */
export function RouterStatus() {
  const { data, isError } = useRouterObserved();
  const unreachable = isError || data?.reachable === false;
  const label = !data && !isError
    ? "Checking router…"
    : unreachable
      ? "Router unreachable"
      : data?.model_name
        ? `${data.model_name} · r${data.revision}`
        : "Router idle";

  return (
    <SidebarMenuButton
      size="lg"
      tooltip={label}
      className="cursor-default hover:bg-transparent active:bg-transparent group-data-[collapsible=icon]:p-0!"
    >
      <span
        className={cn(
          "flex size-8 shrink-0 items-center justify-center rounded-md border",
          unreachable ? "border-danger/30 text-danger" : "border-success/25 text-success",
        )}
      >
        {unreachable ? <Unplug className="size-4" /> : <Radio className="size-4" />}
      </span>
      <span className="grid min-w-0 flex-1 text-left leading-tight group-data-[collapsible=icon]:hidden">
        <span className="flex items-center gap-1.5 text-xs font-medium">
          <span
            className={cn(
              "size-1.5 rounded-full",
              unreachable ? "bg-danger" : "live-dot bg-success text-success",
            )}
          />
          {unreachable ? "Router down" : "Router online"}
        </span>
        <span className="truncate font-mono text-[11px] text-muted-foreground">{label}</span>
      </span>
    </SidebarMenuButton>
  );
}
