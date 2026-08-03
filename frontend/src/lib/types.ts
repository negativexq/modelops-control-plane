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
