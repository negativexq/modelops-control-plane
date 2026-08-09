import type { PolicyEvaluationResult } from "@/lib/types";

// Same palette as StatusBadge's EVALUATING/INCONCLUSIVE amber and DeltaCell's
// better/worse colors: PASS=good=emerald, FAIL=bad=red, INCONCLUSIVE="couldn't
// tell"=amber - never lumped in with PASS, per the policy engine's own precedence
// rule (see docs/DESIGN_NOTES.md#policy-engine).
const STYLES: Record<PolicyEvaluationResult, string> = {
  PASS: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  FAIL: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  INCONCLUSIVE: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
};

export function PolicyResultBadge({ result }: { result: PolicyEvaluationResult }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STYLES[result]}`}
    >
      {result}
    </span>
  );
}
