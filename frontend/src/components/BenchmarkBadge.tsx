/** Marks a deployment created by the benchmark suite (model_name prefixed
 * "benchmark-") so it doesn't get mistaken for a real rollout - see
 * DeploymentOut.is_benchmark. Deliberately a distinct color from StatusBadge/
 * PolicyResultBadge's PASS/FAIL/INCONCLUSIVE palette, since this isn't a result. */
export function BenchmarkBadge() {
  return (
    <span
      title="Created by the benchmark suite, not a real rollout"
      className="inline-flex items-center rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-medium tracking-wide text-violet-800 uppercase dark:bg-violet-950 dark:text-violet-200"
    >
      Benchmark
    </span>
  );
}
