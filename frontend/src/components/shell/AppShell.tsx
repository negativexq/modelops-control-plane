"use client";

import type { ReactNode } from "react";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "./AppHeader";
import { AppSidebar } from "./AppSidebar";
import { CommandPaletteProvider } from "./CommandPalette";

export function AppShell({ children, defaultOpen }: { children: ReactNode; defaultOpen: boolean }) {
  return (
    <SidebarProvider defaultOpen={defaultOpen}>
      <CommandPaletteProvider>
        <AppSidebar />
        <SidebarInset className="min-w-0">
          <AppHeader />
          <div className="mx-auto w-full min-w-0 max-w-7xl flex-1 px-4 py-6 sm:px-6">{children}</div>
        </SidebarInset>
      </CommandPaletteProvider>
    </SidebarProvider>
  );
}
