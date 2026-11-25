import { FlaskConical } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** Marks a deployment created by the benchmark suite (model_name prefixed
 * "benchmark-") so it isn't mistaken for a real rollout - see
 * DeploymentOut.is_benchmark. Outline-only on purpose: it's provenance, not a result. */
export function BenchmarkBadge() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex h-5 items-center gap-1 rounded-full border border-border px-1.5 text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
          <FlaskConical className="size-3" aria-hidden />
          Bench
        </span>
      </TooltipTrigger>
      <TooltipContent>Created by the benchmark suite, not a real rollout</TooltipContent>
    </Tooltip>
  );
}
