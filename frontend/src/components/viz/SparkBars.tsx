import { cn } from "@/lib/utils";

/** Tiny bar sparkline. Empty days render as a faint stub so the time axis stays readable. */
export function SparkBars({
  values,
  className,
  barClassName = "bg-foreground/60",
  title,
}: {
  values: number[];
  className?: string;
  barClassName?: string;
  title?: string;
}) {
  const max = Math.max(1, ...values);
  return (
    <div className={cn("flex h-8 items-end gap-[3px]", className)} title={title} aria-hidden>
      {values.map((v, i) => (
        <span
          key={i}
          className={cn("w-full min-w-[3px] rounded-[2px] transition-[height] duration-500", v > 0 ? barClassName : "bg-foreground/8")}
          style={{ height: v > 0 ? `${Math.max(18, (v / max) * 100)}%` : "12%" }}
        />
      ))}
    </div>
  );
}
