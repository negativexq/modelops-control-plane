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
        <SidebarInset>
          <AppHeader />
          <div className="mx-auto w-full max-w-7xl flex-1 px-6 py-6">{children}</div>
        </SidebarInset>
      </CommandPaletteProvider>
    </SidebarProvider>
  );
}
