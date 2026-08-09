import { PolicyResultBadge } from "@/components/PolicyResultBadge";
import { formatDate } from "@/lib/format";
import type { TimelineItem, TimelinePolicyItem } from "@/lib/types";

const POLICY_LABELS: Record<string, string> = {
  minimum_requests: "Minimum requests",
  latency_p95_increase: "Latency (p95 increase)",
  max_error_rate: "Error rate",
  minimum_recall: "Recall",
};

function formatMetricValue(policyName: string, value: number | null): string {
  if (value == null) return "N/A";
  if (policyName === "minimum_requests") return `${value.toFixed(0)} requests`;
  if (policyName === "minimum_recall") return value.toFixed(2);
  return `${value.toFixed(2)}%`;
}

function PolicyTimelineRow({ item }: { item: TimelinePolicyItem }) {
  return (
    <li className="flex items-start gap-3">
      <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-zinc-400 dark:bg-zinc-600" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-medium">{POLICY_LABELS[item.policy_name] ?? item.policy_name}</span>
          <PolicyResultBadge result={item.result} />
          {item.is_estimated ? (
            <span
              className="inline-flex items-center rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-medium tracking-wide text-zinc-600 uppercase dark:bg-zinc-800 dark:text-zinc-400"
              title="Recorded before traffic-context snapshots existed - this explanation falls back to the deployment's current traffic split, not what it was at evaluation time."
            >
              Estimated
            </span>
          ) : null}
          <span className="text-xs text-zinc-500 dark:text-zinc-400">{formatDate(item.timestamp)}</span>
        </div>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          observed {formatMetricValue(item.policy_name, item.observed_value)} · threshold{" "}
          {formatMetricValue(item.policy_name, item.threshold)}
        </p>
        <p className="mt-0.5 text-zinc-700 dark:text-zinc-300">{item.explanation}</p>
      </div>
    </li>
  );
}

/** The unified deployment history: DeploymentEvent (state transitions, worker
 * actions) and PolicyEvaluation (per-check results, with a derived explanation)
 * merged into one chronological list by GET /api/deployments/{id}/timeline - see
 * docs/DESIGN_NOTES.md#automated-promotion--rollback for why both matter together:
 * an event says *what* happened, a policy evaluation says *why*. */
export function Timeline({ items }: { items: TimelineItem[] }) {
  if (items.length === 0) {
    return <p className="text-sm text-zinc-500 dark:text-zinc-400">No timeline entries yet.</p>;
  }

  return (
    <ol className="space-y-3 text-sm">
      {items.map((item) =>
        item.type === "event" ? (
          <li key={`event-${item.id}`} className="flex items-start gap-3">
            <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-blue-500" aria-hidden />
            <div className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium">{item.event_type}</span>
                <span className="text-xs text-zinc-500 dark:text-zinc-400">
                  {formatDate(item.timestamp)}
                </span>
              </div>
              <p className="text-zinc-700 dark:text-zinc-300">{item.message}</p>
            </div>
          </li>
        ) : (
          <PolicyTimelineRow key={`policy-${item.id}`} item={item} />
        ),
      )}
    </ol>
  );
}
