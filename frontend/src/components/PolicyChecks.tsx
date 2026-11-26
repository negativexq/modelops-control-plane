import { latestPolicyResults } from "@/lib/derive";
import { formatDate } from "@/lib/format";
import type { TimelineItem } from "@/lib/types";
import { PolicyResultBadge } from "@/components/PolicyResultBadge";
import { ConfidenceStrip } from "@/components/viz/ConfidenceStrip";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

function fmt(policy: string, v: number | null) {
  if (v == null) return "N/A";
  if (policy === "minimum_label_coverage") return `${(v * 100).toFixed(0)}%`;
  if (policy.startsWith("minimum_") && policy !== "minimum_recall") return v.toFixed(0);
  if (policy === "minimum_recall") return v.toFixed(2);
  if (policy === "feature_drift" || policy === "prediction_drift") return `PSI ${v.toFixed(2)}`;
  return `${v.toFixed(2)}%`;
}

/** Latest verdict per policy - the evidence behind the current/last decision -
 * with how many times each policy has been evaluated over the rollout. The
 * Evidence column draws each check's confidence interval against its limit
 * (see app/policy/stats.py): a check only FAILs when the whole bar is past it. */
export function PolicyChecks({ items }: { items: TimelineItem[] }) {
  const latest = latestPolicyResults(items);
  const runs = new Map<string, number>();
  for (const i of items) if (i.type === "policy_evaluation") runs.set(i.policy_name, (runs.get(i.policy_name) ?? 0) + 1);

  if (latest.length === 0) return <p className="text-sm text-muted-foreground">No policy has been evaluated yet.</p>;

  return (
    <div className="overflow-hidden rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow className="bg-muted/40 hover:bg-muted/40">
            <TableHead>Policy</TableHead>
            <TableHead>Result</TableHead>
            <TableHead className="text-right">Observed</TableHead>
            <TableHead className="text-right">Threshold</TableHead>
            <TableHead className="text-right" title="Confidence interval (bar), observed value (dot), limit (dashed tick)">
              Evidence
            </TableHead>
            <TableHead className="text-right">Runs</TableHead>
            <TableHead>Why</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {latest.map((p) => (
            <TableRow key={p.id}>
              <TableCell className="font-mono text-xs">{p.policy_name}</TableCell>
              <TableCell>
                <PolicyResultBadge result={p.result} />
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular">
                {fmt(p.policy_name, p.observed_value)}
                {/* feature_drift names the feature it's about (or why it couldn't compare). */}
                {p.policy_name === "feature_drift" && p.observed_value != null && p.detail ? (
                  <span className="block text-[10px] text-muted-foreground">{p.detail}</span>
                ) : null}
              </TableCell>
              <TableCell className="text-right font-mono text-xs text-muted-foreground tabular">
                {fmt(p.policy_name, p.threshold)}
              </TableCell>
              <TableCell className="text-right">
                <ConfidenceStrip check={p} format={(v) => fmt(p.policy_name, v)} className="ml-auto" />
              </TableCell>
              <TableCell className="text-right font-mono text-xs text-muted-foreground tabular" title={`last ${formatDate(p.timestamp)}`}>
                {runs.get(p.policy_name)}
              </TableCell>
              <TableCell className="max-w-md text-xs whitespace-normal text-muted-foreground">{p.explanation}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
