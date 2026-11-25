import { cn } from "@/lib/utils";
import { POLICY_TONE, TONE_PILL } from "@/lib/status";
import type { PolicyEvaluationResult } from "@/lib/types";

// PASS=success, FAIL=danger, INCONCLUSIVE=warning - never lumped in with PASS,
// per the policy engine's own precedence rule (docs/DESIGN_NOTES.md#policy-engine).
export function PolicyResultBadge({ result }: { result: PolicyEvaluationResult }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded-full border px-2 font-mono text-[10px] font-semibold tracking-wide uppercase",
        TONE_PILL[POLICY_TONE[result]],
      )}
    >
      {result}
    </span>
  );
}
