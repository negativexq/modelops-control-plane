export type DeploymentStatus =
  | "PENDING"
  | "DEPLOYING"
  | "CANARY_RUNNING"
  | "EVALUATING"
  | "PROMOTING"
  | "PROMOTED"
  | "ROLLING_BACK"
  | "ROLLED_BACK"
  | "FAILED"
  | "INCONCLUSIVE";

export interface TargetWeight {
  version: string;
  weight: number;
}

export interface TrafficAllocationOut {
  targets: TargetWeight[];
  // Desired-state revision - what the router *should* have applied. Compared
  // against ObservedRouterState.revision for drift detection - see
  // backend/docs/DESIGN_NOTES.md#desired-observed-reconciliation.
  revision: number;
  updated_at: string;
}

// GET /api/router/observed - read-only passthrough of the router's own
// GET /router/config (never mutates anything, unlike POST /api/router/reconcile).
// Check `reachable` first: a genuinely unreachable router and a reachable-but-
// never-configured one (fresh restart) both report deployment_id: null, so
// only `reachable` tells them apart.
export interface ObservedRouterState {
  reachable: boolean;
  model_name: string | null;
  deployment_id: string | null;
  revision: number | null;
  targets: TargetWeight[] | null;
}

export interface DeploymentEventOut {
  id: string;
  event_type: string;
  message: string;
  created_at: string;
}

export interface DeploymentOut {
  id: string;
  model_name: string;
  stable_version: string;
  canary_version: string;
  status: DeploymentStatus;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  traffic_allocation: TrafficAllocationOut | null;
  events: DeploymentEventOut[];
  // Derived (model_name starts with "benchmark-"), not a stored column - see
  // backend/app/control_plane/schemas.py's DeploymentOut.is_benchmark.
  is_benchmark: boolean;
  // Manual automation hold - when true, the worker skips this deployment
  // entirely (see backend/app/worker/loop.py's run_once). Manual
  // promote/rollback/evaluate are unaffected. See
  // backend/docs/DESIGN_NOTES.md#manual-automation-hold.
  automation_paused: boolean;
}

export type PolicyEvaluationResult = "PASS" | "FAIL" | "INCONCLUSIVE";

// --- Timeline ------------------------------------------------------------------
// GET /api/deployments/{id}/timeline - DeploymentEvent and PolicyEvaluation rows
// merged into one chronological narrative. Discriminated on `type`.

export interface TimelineEventItem {
  type: "event";
  id: string;
  timestamp: string;
  event_type: string;
  message: string;
}

export interface TimelinePolicyItem {
  type: "policy_evaluation";
  id: string;
  timestamp: string;
  policy_name: string;
  metric_name: string;
  observed_value: number | null;
  threshold: number | null;
  result: PolicyEvaluationResult;
  // Human-readable "why" for this result, derived server-side - see
  // backend/app/policy/explain.py. Especially useful for INCONCLUSIVE, which
  // otherwise reads as an unexplained non-answer.
  explanation: string;
  // True only for checks recorded before the traffic-context snapshot columns
  // existed on PolicyEvaluation - `explanation` then falls back to the
  // deployment's *current* traffic split rather than what was true at evaluation
  // time (see backend/app/control_plane/timeline.py).
  is_estimated: boolean;
}

export type TimelineItem = TimelineEventItem | TimelinePolicyItem;

export interface CreateDeploymentRequest {
  model_name: string;
  stable_version: string;
  canary_version: string;
  canary_weight: number;
  // Internal-only escape hatch for the benchmark suite's "baseline" scenario
  // (canary_weight=0) - never set by this dashboard's own NewDeploymentForm, which
  // already enforces 0 < weight < 1 client-side. See backend's
  // CreateDeploymentRequest for the validation this bypasses.
  allow_degenerate_canary_weight?: boolean;
}

export interface MetricsSummary {
  version: string;
  sample_count: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p99_latency_ms: number | null;
  error_rate: number | null;
  // null only when sample_count is itself 0 - a real 0 means samples exist but
  // none are labeled yet (see backend/app/control_plane/schemas.py's
  // MetricsSummary docstring).
  precision: number | null;
  recall: number | null;
  false_positive_rate: number | null;
  labeled_sample_count: number;
  label_coverage: number | null;
  // How many of `labeled_sample_count` are the positive class - recall's real
  // denominator (TP+FN), not `labeled_sample_count` itself. See
  // backend/app/policy/engine.py's minimum_positive_labels gate.
  positive_label_count: number;
  label_delay_p50_seconds: number | null;
  label_delay_p95_seconds: number | null;
}

export interface MetricsOut {
  window_seconds: number;
  stable: MetricsSummary;
  canary: MetricsSummary;
}

export interface MetricsDeltas {
  p95_latency_ms: number | null;
  error_rate: number | null;
  recall: number | null;
}

export interface ComparisonOut {
  window_seconds: number;
  stable: MetricsSummary;
  canary: MetricsSummary;
  deltas: MetricsDeltas;
}

// The local model registry's metadata.json / evaluation.json are free-form JSON
// (see backend/scripts/train_models.py) - these list the fields the dashboard
// actually reads, plus an index signature for the rest.
export interface ModelVersionMetadata {
  model_name?: string;
  version?: string;
  role?: string;
  algorithm?: string;
  trained_at?: string;
  features?: string[];
  hyperparameters?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ModelVersionEvaluation {
  precision?: number;
  recall?: number;
  f1?: number;
  false_positive_rate?: number;
  roc_auc?: number;
  [key: string]: unknown;
}

// --- Benchmarks --------------------------------------------------------------

export type BenchmarkScenarioKey =
  | "baseline"
  | "latency-failure"
  | "error-failure"
  | "quality-failure"
  | "success";

export interface ScenarioInfo {
  key: BenchmarkScenarioKey;
  title: string;
  description: string;
  expected_outcome: string;
  // Non-null only for quality-failure/success: those scenarios use deliberately
  // loosened thresholds so harness noise can't false-trigger a rollback - see
  // backend/scripts/benchmarks/scenarios.py. Ground-truth labels for both flow
  // through the platform's real ingestion path (POST /api/labels), delayed, not
  // a direct DB write - but their source is still the synthetic test dataset's
  // known labels, not real production traffic.
  synthetic_disclaimer: string | null;
}

export type BenchmarkRunStatus = "RUNNING" | "COMPLETED" | "FAILED";

// Mirrors backend/scripts/benchmarks/report.py's LoadTestResult dataclass.
export interface LoadTestResult {
  total_requests: number;
  total_failures: number;
  requests_per_second: number;
  error_rate: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  duration_seconds: number;
}

// Mirrors backend/scripts/benchmarks/report.py's BenchmarkResult dataclass - this is
// what BenchmarkRun.result holds once a run COMPLETEs (see save_json_report).
export interface BenchmarkResult {
  scenario: string;
  description: string;
  expected_outcome: string;
  observed_outcome: string;
  outcome_matches_expectation: boolean;
  deployment_id: string;
  model_name: string;
  started_at: string | null;
  final_status: string;
  load: LoadTestResult | null;
  time_to_detect_seconds: number | null;
  time_to_action_seconds: number | null;
  notes: string[];
  run_at: string;
}

export interface BenchmarkRun {
  id: string;
  scenario: string;
  status: BenchmarkRunStatus;
  started_at: string;
  completed_at: string | null;
  result: BenchmarkResult | null;
  error_message: string | null;
}

export interface RunBenchmarkRequest {
  scenario: string;
  duration_seconds?: number;
  max_wait_seconds?: number;
}
