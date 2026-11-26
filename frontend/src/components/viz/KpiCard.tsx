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
        "relative flex flex-col gap-3 overflow-hidden rounded-xl border bg-card p-4",
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
      {/* Fixed rows - label, value + chart, a two-line hint slot - so the numbers
          and sparklines line up across a row of cards whatever the hint length. */}
      <div className="flex h-9 items-end justify-between gap-4">
        <p className="text-3xl leading-none font-semibold tracking-tight tabular">
          <NumberTicker value={value} />
        </p>
        {chart ? <div className="w-24 shrink-0">{chart}</div> : null}
      </div>
      <p className="line-clamp-2 min-h-[2lh] text-xs text-muted-foreground" title={typeof hint === "string" ? hint : undefined}>
        {hint}
      </p>
    </div>
  );
}
