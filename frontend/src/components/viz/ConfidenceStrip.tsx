import type { TimelinePolicyItem } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * One policy check's statistical evidence on a single axis: the confidence
 * interval as a bar, the observed value as a dot, the policy limit as a tick.
 * A FAIL is a bar entirely past the tick; INCONCLUSIVE is a dot past the tick
 * whose bar still reaches back across it - the breach could be noise.
 */

const TONE = {
  PASS: "var(--success)",
  FAIL: "var(--danger)",
  INCONCLUSIVE: "var(--warning)",
} as const;

const W = 132;
const H = 18;
const PAD = 5;

export function formatPValue(p: number) {
  if (p < 0.001) return "p < 0.001";
  return `p = ${p.toFixed(p < 0.01 ? 3 : 2)}`;
}

export function ConfidenceStrip({
  check,
  format,
  className,
}: {
  check: Pick<TimelinePolicyItem, "ci_low" | "ci_high" | "observed_value" | "threshold" | "result" | "p_value" | "confidence_level">;
  format: (v: number) => string;
  className?: string;
}) {
  const { ci_low: low, ci_high: high, observed_value: observed, threshold } = check;
  if (low == null || high == null || observed == null || threshold == null) {
    return <span className="text-xs text-muted-foreground">-</span>;
  }

  const values = [low, high, observed, threshold];
  let min = Math.min(...values);
  let max = Math.max(...values);
  const span = max - min || Math.abs(max) || 1;
  min -= span * 0.08;
  max += span * 0.08;
  const x = (v: number) => PAD + ((v - min) / (max - min)) * (W - 2 * PAD);
  const tone = TONE[check.result];
  const level = check.confidence_level != null ? `${Math.round(check.confidence_level * 100)}%` : "";
  const label = `${level} CI ${format(low)} to ${format(high)}${check.p_value != null ? `, ${formatPValue(check.p_value)}` : ""}`;

  return (
    <div className={cn("flex flex-col items-end gap-0.5", className)} title={label}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} className="overflow-visible">
        <line x1={PAD} x2={W - PAD} y1={H / 2} y2={H / 2} stroke="var(--border)" strokeWidth={1} />
        <rect
          x={x(low)}
          y={H / 2 - 3}
          width={Math.max(2, x(high) - x(low))}
          height={6}
          rx={3}
          fill={tone}
          fillOpacity={0.28}
          stroke={tone}
          strokeOpacity={0.6}
        />
        <line x1={x(threshold)} x2={x(threshold)} y1={1} y2={H - 1} stroke="var(--foreground)" strokeOpacity={0.55} strokeDasharray="2 2" />
        <circle cx={x(observed)} cy={H / 2} r={3} fill={tone} />
      </svg>
      <span className="font-mono text-[10px] leading-none text-muted-foreground tabular">
        {format(low)}–{format(high)}
        {check.p_value != null ? ` · ${formatPValue(check.p_value)}` : ""}
      </span>
    </div>
  );
}
