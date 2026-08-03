import type { TargetWeight } from "@/lib/types";

const COLORS = ["bg-blue-600", "bg-violet-600", "bg-amber-500", "bg-emerald-600"];

export function TrafficBar({ targets }: { targets: TargetWeight[] }) {
  const total = targets.reduce((sum, t) => sum + t.weight, 0) || 1;

  return (
    <div className="space-y-2">
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
        {targets.map((target, index) => (
          <div
            key={target.version}
            className={COLORS[index % COLORS.length]}
            style={{ width: `${(target.weight / total) * 100}%` }}
            title={`${target.version}: ${((target.weight / total) * 100).toFixed(1)}%`}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-600 dark:text-zinc-400">
        {targets.map((target, index) => (
          <span key={target.version} className="inline-flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${COLORS[index % COLORS.length]}`} />
            {target.version} — {((target.weight / total) * 100).toFixed(1)}%
          </span>
        ))}
      </div>
    </div>
  );
}
