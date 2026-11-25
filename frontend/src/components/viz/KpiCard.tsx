import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { NumberTicker } from "./NumberTicker";

export function KpiCard({
  label,
  value,
  icon: Icon,
  tone = "text-muted-foreground",
  hint,
  chart,
  emphasis = false,
}: {
  label: string;
  value: number;
  icon: LucideIcon;
  tone?: string;
  hint?: ReactNode;
  chart?: ReactNode;
  emphasis?: boolean;
}) {
  return (
    <div
      className={cn(
        "relative flex flex-col justify-between gap-3 overflow-hidden rounded-xl border bg-card p-4",
        emphasis && "border-canary/30",
      )}
    >
      {emphasis ? (
        <span aria-hidden className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-canary to-transparent" />
      ) : null}
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Icon className={cn("size-3.5", tone)} aria-hidden />
        {label}
      </div>
      <div className="flex items-end justify-between gap-4">
        <div className="space-y-1">
          <p className="text-3xl font-semibold tracking-tight">
            <NumberTicker value={value} />
          </p>
          {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
        </div>
        {chart ? <div className="w-24 shrink-0">{chart}</div> : null}
      </div>
    </div>
  );
}
