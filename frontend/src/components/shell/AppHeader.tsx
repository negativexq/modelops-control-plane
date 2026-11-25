"use client";

import Link from "next/link";
import { Fragment } from "react";
import { usePathname } from "next/navigation";
import { useIsFetching } from "@tanstack/react-query";
import { useDeployment } from "@/lib/queries";
import { cn } from "@/lib/utils";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { NAV_ITEMS } from "./nav";
import { ThemeToggle } from "./ThemeToggle";

function useCrumbs(): Array<{ href: string; label: string; mono?: boolean }> {
  const pathname = usePathname();
  const segments = pathname.split("/").filter(Boolean);
  // Always called (hooks can't be conditional); disabled unless on a detail page.
  const detailId = segments[0] === "deployments" ? segments[1] : undefined;
  const { data: deployment } = useDeployment(detailId ?? "", detailId != null);
  if (segments.length === 0) return [{ href: "/", label: "Overview" }];

  const crumbs: Array<{ href: string; label: string; mono?: boolean }> = [];
  const section = NAV_ITEMS.find((item) => item.href === `/${segments[0]}`);
  crumbs.push({ href: `/${segments[0]}`, label: section?.label ?? segments[0] });

  if (segments[0] === "deployments" && segments[1]) {
    crumbs.push({
      href: pathname,
      label: deployment ? `${deployment.model_name} · ${deployment.canary_version}` : segments[1].slice(0, 8),
      mono: true,
    });
  }
  return crumbs;
}

/** Tiny "the data on screen is live" signal: pulses while any query is in flight. */
function SyncIndicator() {
  const fetching = useIsFetching();
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span
            className={cn(
              "size-1.5 rounded-full transition-colors",
              fetching > 0 ? "bg-canary" : "bg-success",
            )}
          />
          Live
        </span>
      </TooltipTrigger>
      <TooltipContent>Auto-refreshing. Active rollouts poll every few seconds.</TooltipContent>
    </Tooltip>
  );
}

export function AppHeader() {
  const crumbs = useCrumbs();
  return (
    <header className="sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b bg-background/80 px-4 backdrop-blur supports-backdrop-filter:bg-background/60">
      <SidebarTrigger className="-ml-1" />
      <Separator orientation="vertical" className="mr-1 data-[orientation=vertical]:h-4" />
      <Breadcrumb>
        <BreadcrumbList>
          {crumbs.map((crumb, index) => (
            <Fragment key={crumb.href}>
              {index > 0 ? <BreadcrumbSeparator /> : null}
              <BreadcrumbItem>
                {index === crumbs.length - 1 ? (
                  <BreadcrumbPage className={cn(crumb.mono && "font-mono text-xs")}>{crumb.label}</BreadcrumbPage>
                ) : (
                  <BreadcrumbLink asChild>
                    <Link href={crumb.href}>{crumb.label}</Link>
                  </BreadcrumbLink>
                )}
              </BreadcrumbItem>
            </Fragment>
          ))}
        </BreadcrumbList>
      </Breadcrumb>
      <div className="ml-auto flex items-center gap-3">
        <SyncIndicator />
        <ThemeToggle />
      </div>
    </header>
  );
}
