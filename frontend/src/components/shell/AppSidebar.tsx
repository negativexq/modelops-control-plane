"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Search } from "lucide-react";
import { isLive, useDeployments } from "@/lib/queries";
import { STATUS_META, TONE_DOT } from "@/lib/status";
import { cn } from "@/lib/utils";
import { Kbd } from "@/components/ui/kbd";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import { Logo } from "./Logo";
import { NAV_ITEMS, isActivePath } from "./nav";
import { RouterStatus } from "./RouterStatus";
import { useCommandPalette } from "./CommandPalette";

export function AppSidebar() {
  const pathname = usePathname();
  const { setOpen } = useCommandPalette();
  const { data: deployments } = useDeployments();
  const live = (deployments ?? []).filter((d) => isLive(d.status) || d.status === "INCONCLUSIVE");

  return (
    <Sidebar collapsible="icon" variant="sidebar">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" asChild tooltip="ModelOps" className="group-data-[collapsible=icon]:p-0!">
              <Link href="/">
                <Logo className="size-8 shrink-0" />
                <span className="grid leading-tight group-data-[collapsible=icon]:hidden">
                  <span className="text-sm font-semibold tracking-tight">ModelOps</span>
                  <span className="text-[11px] text-muted-foreground">Control plane</span>
                </span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip="Search (⌘K)"
              onClick={() => setOpen(true)}
              className="border bg-background/40 text-muted-foreground shadow-none"
            >
              <Search />
              <span className="flex-1">Search…</span>
              <Kbd className="group-data-[collapsible=icon]:hidden">⌘K</Kbd>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Operate</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV_ITEMS.map((item) => (
                <SidebarMenuItem key={item.href}>
                  <SidebarMenuButton asChild isActive={isActivePath(pathname, item.href)} tooltip={item.label}>
                    <Link href={item.href}>
                      <item.icon />
                      <span>{item.label}</span>
                    </Link>
                  </SidebarMenuButton>
                  {item.href === "/deployments" && live.length > 0 ? (
                    <SidebarMenuBadge className="text-canary">{live.length}</SidebarMenuBadge>
                  ) : null}
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup className="group-data-[collapsible=icon]:hidden">
          <SidebarGroupLabel>Needs attention</SidebarGroupLabel>
          <SidebarGroupContent>
            {live.length === 0 ? (
              <p className="px-2 text-xs text-muted-foreground">No rollout in flight.</p>
            ) : (
              <SidebarMenu>
                {live.slice(0, 8).map((d) => {
                  const meta = STATUS_META[d.status];
                  return (
                    <SidebarMenuItem key={d.id}>
                      <SidebarMenuButton
                        asChild
                        size="sm"
                        isActive={pathname === `/deployments/${d.id}`}
                      >
                        <Link href={`/deployments/${d.id}`} title={`${d.model_name} · ${meta.label}`}>
                          <span
                            className={cn(
                              "size-1.5 shrink-0 rounded-full",
                              TONE_DOT[meta.tone],
                              isLive(d.status) && "live-dot",
                            )}
                          />
                          <span className="truncate font-mono text-xs">{d.model_name}</span>
                          <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                            {d.canary_version}
                          </span>
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            )}
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <RouterStatus />
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}
