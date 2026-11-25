"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useRouter } from "next/navigation";
import { Monitor, Moon, PanelLeft, Rocket, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useDeployments, useModels } from "@/lib/queries";
import { STATUS_META, TONE_DOT } from "@/lib/status";
import { cn } from "@/lib/utils";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";
import { useSidebar } from "@/components/ui/sidebar";
import { NAV_ITEMS } from "./nav";

const PaletteContext = createContext<{
  open: boolean;
  setOpen: (open: boolean) => void;
} | null>(null);

export function useCommandPalette() {
  const ctx = useContext(PaletteContext);
  if (!ctx)
    throw new Error(
      "useCommandPalette must be used inside <CommandPaletteProvider>",
    );
  return ctx;
}

export function CommandPaletteProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setOpen((prev) => !prev);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const value = useMemo(() => ({ open, setOpen }), [open]);
  return (
    <PaletteContext.Provider value={value}>
      {children}
      <CommandPalette />
    </PaletteContext.Provider>
  );
}

function CommandPalette() {
  const { open, setOpen } = useCommandPalette();
  const router = useRouter();
  const { setTheme } = useTheme();
  const { toggleSidebar } = useSidebar();
  const { data: deployments } = useDeployments();
  const { data: models } = useModels();

  const run = useCallback(
    (action: () => void) => {
      setOpen(false);
      action();
    },
    [setOpen],
  );

  return (
    <CommandDialog
      open={open}
      onOpenChange={setOpen}
      className="sm:max-w-2xl"
      title="Command palette"
      description="Jump anywhere or run an action"
    >
      <Command>
        <CommandInput placeholder="Search deployments, models, pages…" />
        <CommandList>
          <CommandEmpty>Nothing matches.</CommandEmpty>

          <CommandGroup heading="Navigate">
            {NAV_ITEMS.map((item) => (
              <CommandItem
                key={item.href}
                value={`page ${item.label}`}
                onSelect={() => run(() => router.push(item.href))}
              >
                <item.icon />
                <span>{item.label}</span>
                <span className="ml-2 truncate text-xs text-muted-foreground">
                  {item.description}
                </span>
              </CommandItem>
            ))}
          </CommandGroup>

          {deployments && deployments.length > 0 ? (
            <>
              <CommandSeparator />
              <CommandGroup heading="Deployments">
                {deployments.slice(0, 30).map((d) => {
                  const meta = STATUS_META[d.status];
                  return (
                    <CommandItem
                      key={d.id}
                      value={`deployment ${d.model_name} ${d.stable_version} ${d.canary_version} ${meta.label} ${d.id}`}
                      onSelect={() =>
                        run(() => router.push(`/deployments/${d.id}`))
                      }
                    >
                      <span
                        className={cn(
                          "size-1.5 rounded-full",
                          TONE_DOT[meta.tone],
                        )}
                      />
                      <span className="truncate font-mono text-xs">{d.model_name}</span>
                      <span className="font-mono text-xs text-muted-foreground">
                        {d.stable_version} → {d.canary_version}
                      </span>
                      <CommandShortcut className="whitespace-nowrap tracking-normal">
                        {meta.label}
                      </CommandShortcut>
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            </>
          ) : null}

          <CommandSeparator />
          <CommandGroup heading="Actions">
            {(models ?? []).map((model) => (
              <CommandItem
                key={model}
                value={`start rollout ${model}`}
                onSelect={() => run(() => router.push("/models"))}
              >
                <Rocket />
                Start a canary rollout for{" "}
                <span className="font-mono">{model}</span>
              </CommandItem>
            ))}
            <CommandItem
              value="toggle sidebar"
              onSelect={() => run(toggleSidebar)}
            >
              <PanelLeft /> Toggle sidebar
              <CommandShortcut>⌘B</CommandShortcut>
            </CommandItem>
            <CommandItem
              value="theme dark"
              onSelect={() => run(() => setTheme("dark"))}
            >
              <Moon /> Dark theme
            </CommandItem>
            <CommandItem
              value="theme light"
              onSelect={() => run(() => setTheme("light"))}
            >
              <Sun /> Light theme
            </CommandItem>
            <CommandItem
              value="theme system"
              onSelect={() => run(() => setTheme("system"))}
            >
              <Monitor /> System theme
            </CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>
    </CommandDialog>
  );
}
