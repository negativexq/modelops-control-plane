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
  updated_at: string;
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
}

export interface CreateDeploymentRequest {
  model_name: string;
  stable_version: string;
  canary_version: string;
  canary_weight: number;
}

export interface MetricsSummary {
  version: string;
  sample_count: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p99_latency_ms: number | null;
  error_rate: number | null;
  precision: number | null;
  recall: number | null;
  false_positive_rate: number | null;
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
  // loosened thresholds and (for success) synthetic ground-truth backfill, which is
  // not real platform behavior - see backend/scripts/benchmarks/scenarios.py.
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
