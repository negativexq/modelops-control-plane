import { cn } from "@/lib/utils";
import type { TargetWeight } from "@/lib/types";

/** Stable is always the first target (see TrafficAllocation ordering); everything
 * after it is canary traffic. Colours follow that role, not the index, so a
 * promoted deployment reads as "all canary-blue" rather than an arbitrary hue. */
export function TrafficBar({
  targets,
  stableVersion,
  size = "md",
  showLegend = true,
}: {
  targets: TargetWeight[];
  stableVersion?: string;
  size?: "sm" | "md";
  showLegend?: boolean;
}) {
  const total = targets.reduce((sum, t) => sum + t.weight, 0) || 1;
  const isStable = (t: TargetWeight, i: number) =>
    stableVersion ? t.version === stableVersion : i === 0;

  return (
    <div className="space-y-2">
      <div
        className={cn(
          "flex w-full gap-0.5 overflow-hidden rounded-full bg-muted",
          size === "sm" ? "h-1.5" : "h-2.5",
        )}
      >
        {targets
          .filter((t) => t.weight > 0)
          .map((target) => {
            const index = targets.indexOf(target);
            return (
              <div
                key={target.version}
                className={cn(
                  "h-full transition-[width] duration-700 ease-out",
                  isStable(target, index) ? "bg-stable" : "bg-canary",
                )}
                style={{ width: `${(target.weight / total) * 100}%` }}
                title={`${target.version}: ${((target.weight / total) * 100).toFixed(1)}%`}
              />
            );
          })}
      </div>
      {showLegend ? (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {targets.map((target, index) => (
            <span key={target.version} className="inline-flex items-center gap-1.5">
              <span
                className={cn(
                  "size-2 rounded-full",
                  isStable(target, index) ? "bg-stable" : "bg-canary",
                )}
              />
              <span className="font-mono text-foreground">{target.version}</span>
              <span className="tabular">{((target.weight / total) * 100).toFixed(1)}%</span>
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
