import {
  ArrowUpRight,
  CircleDot,
  Gauge,
  PauseCircle,
  PlayCircle,
  Radio,
  RadioTower,
  RefreshCw,
  Rocket,
  ShieldCheck,
  ShieldQuestion,
  ShieldX,
  Undo2,
  UserRound,
  Unplug,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { POLICY_TONE, TONE_TEXT, type Tone } from "@/lib/status";
import { formatDate } from "@/lib/format";
import type { TimelineItem, TimelinePolicyItem } from "@/lib/types";
import { PolicyResultBadge } from "@/components/PolicyResultBadge";

const POLICY_LABELS: Record<string, string> = {
  minimum_requests: "Minimum requests",
  latency_p95_increase: "Latency (p95 increase)",
  max_error_rate: "Error rate",
  minimum_labeled_samples: "Minimum labeled samples",
  minimum_label_coverage: "Label coverage",
  minimum_positive_labels: "Minimum positive labels",
  minimum_recall: "Recall",
  feature_drift: "Feature drift",
};

function formatMetricValue(policyName: string, value: number | null): string {
  if (value == null) return "N/A";
  if (policyName === "minimum_requests") return `${value.toFixed(0)} req`;
  if (policyName === "minimum_labeled_samples") return `${value.toFixed(0)} labeled`;
  if (policyName === "minimum_positive_labels") return `${value.toFixed(0)} positive`;
  // minimum_label_coverage's observed_value/threshold are 0..1 fractions (same
  // convention as MetricsSummary.label_coverage) - multiply before appending "%",
  // unlike latency/error-rate checks whose values are already percentages.
  if (policyName === "minimum_label_coverage") return `${(value * 100).toFixed(0)}%`;
  if (policyName === "minimum_recall") return value.toFixed(2);
  if (policyName === "feature_drift") return `PSI ${value.toFixed(2)}`;
  return `${value.toFixed(2)}%`;
}

/** Icon + tone per event_type. Unknown types fall back to a neutral dot so new
 * backend events still render. */
function eventVisual(eventType: string): { icon: LucideIcon; tone: Tone } {
  const t = eventType.toLowerCase();
  if (t.includes("router_unreachable")) return { icon: Unplug, tone: "danger" };
  if (t.includes("router_recovered")) return { icon: RadioTower, tone: "success" };
  if (t.includes("reconcil")) return { icon: RefreshCw, tone: "canary" };
  if (t.includes("rolled_back") || t.includes("rollback")) return { icon: Undo2, tone: "danger" };
  if (t.includes("promot")) return { icon: ArrowUpRight, tone: "success" };
  if (t.includes("inconclusive")) return { icon: ShieldQuestion, tone: "warning" };
  if (t.includes("fail")) return { icon: ShieldX, tone: "danger" };
  if (t.includes("pause")) return { icon: PauseCircle, tone: "warning" };
  if (t.includes("resum")) return { icon: PlayCircle, tone: "canary" };
  if (t.includes("traffic") || t.includes("weight")) return { icon: Gauge, tone: "canary" };
  if (t.includes("creat") || t.includes("start")) return { icon: Rocket, tone: "canary" };
  if (t.includes("route")) return { icon: Radio, tone: "canary" };
  return { icon: CircleDot, tone: "neutral" };
}

const POLICY_ICON: Record<TimelinePolicyItem["result"], LucideIcon> = {
  PASS: ShieldCheck,
  FAIL: ShieldX,
  INCONCLUSIVE: ShieldQuestion,
};

function Rail({ icon: Icon, tone, last }: { icon: LucideIcon; tone: Tone; last: boolean }) {
  return (
    <div className="relative flex flex-col items-center">
      <span
        className={cn(
          "z-10 flex size-7 items-center justify-center rounded-full border bg-card",
          TONE_TEXT[tone],
        )}
      >
        <Icon className="size-3.5" aria-hidden />
      </span>
      {last ? null : <span className="w-px flex-1 bg-border" aria-hidden />}
    </div>
  );
}

function Timestamp({ value }: { value: string }) {
  return <time className="font-mono text-[11px] text-muted-foreground tabular">{formatDate(value)}</time>;
}

function PolicyRow({ item, last }: { item: TimelinePolicyItem; last: boolean }) {
  return (
    <li className="flex gap-3">
      <Rail icon={POLICY_ICON[item.result]} tone={POLICY_TONE[item.result]} last={last} />
      <div className="min-w-0 flex-1 pb-5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-medium">{POLICY_LABELS[item.policy_name] ?? item.policy_name}</span>
          <PolicyResultBadge result={item.result} />
          {item.is_estimated ? (
            <span
              className="rounded border border-border px-1.5 text-[10px] tracking-wide text-muted-foreground uppercase"
              title="Recorded before traffic-context snapshots existed - this explanation falls back to the deployment's current traffic split, not what it was at evaluation time."
            >
              Estimated
            </span>
          ) : null}
          <span className="ml-auto">
            <Timestamp value={item.timestamp} />
          </span>
        </div>
        <p className="mt-1 font-mono text-xs text-muted-foreground tabular">
          observed <span className="text-foreground">{formatMetricValue(item.policy_name, item.observed_value)}</span>
          {"  ·  "}threshold {formatMetricValue(item.policy_name, item.threshold)}
        </p>
        <p className="mt-1 text-sm text-muted-foreground">{item.explanation}</p>
      </div>
    </li>
  );
}

/** The unified deployment history: DeploymentEvent (what happened) and
 * PolicyEvaluation (why) merged into one chronological list by
 * GET /api/deployments/{id}/timeline. */
export function Timeline({ items }: { items: TimelineItem[] }) {
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">No timeline entries yet.</p>;
  }

  return (
    <ol className="text-sm">
      {items.map((item, index) => {
        const last = index === items.length - 1;
        if (item.type === "policy_evaluation") {
          return <PolicyRow key={`policy-${item.id}`} item={item} last={last} />;
        }
        const visual = eventVisual(item.event_type);
        return (
          <li key={`event-${item.id}`} className="flex gap-3">
            <Rail icon={visual.icon} tone={visual.tone} last={last} />
            <div className="min-w-0 flex-1 pb-5">
              <div className="flex flex-wrap items-center gap-x-2">
                <span className="font-mono text-xs font-medium">{item.event_type}</span>
                {item.actor ? (
                  <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <UserRound className="size-3" aria-hidden />
                    {item.actor}
                  </span>
                ) : null}
                <span className="ml-auto">
                  <Timestamp value={item.timestamp} />
                </span>
              </div>
              <p className="mt-1 text-muted-foreground">{item.message}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
