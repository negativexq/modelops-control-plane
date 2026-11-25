/** Query keys, shared by queries.ts (pull) and live-stream.ts (push). */
export const keys = {
  deployments: ["deployments"] as const,
  deployment: (id: string) => ["deployments", id] as const,
  timeline: (id: string) => ["deployments", id, "timeline"] as const,
  timeseries: (id: string) => ["deployments", id, "timeseries"] as const,
  policyDefaults: ["policy", "defaults"] as const,
  comparison: (id: string, windowSeconds: number) =>
    ["deployments", id, "comparison", windowSeconds] as const,
  routerObserved: ["router", "observed"] as const,
  fleetSummary: ["fleet", "summary"] as const,
  models: ["models"] as const,
  modelVersions: (model: string) => ["models", model, "versions"] as const,
  modelVersionRows: (model: string) => ["models", model, "rows"] as const,
  scenarios: ["benchmarks", "scenarios"] as const,
  benchmarkRuns: ["benchmarks", "runs"] as const,
  benchmarkCurrent: ["benchmarks", "current"] as const,
};
