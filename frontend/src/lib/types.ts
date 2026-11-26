/**
 * API types. Everything the control plane declares in its OpenAPI schema comes
 * from ./api-schema.ts, which is generated, never edited by hand:
 *
 *   npm run gen:api   (backend schema -> openapi.json -> api-schema.ts)
 *
 * CI regenerates both and fails on any diff, so a backend change can't ship
 * without these types following it. Only shapes the schema can't describe are
 * written out below: free-form registry JSON, the SSE payload, and view-model
 * types the dashboard derives itself.
 */
import type { components } from "./api-schema";

type Schemas = components["schemas"];

// --- Deployments ------------------------------------------------------------------

export type DeploymentStatus = Schemas["DeploymentStatus"];
export type TargetWeight = Schemas["TargetWeight"];
export type TrafficAllocationOut = Schemas["TrafficAllocationOut"];
export type DeploymentEventOut = Schemas["DeploymentEventOut"];
export type DeploymentOut = Schemas["DeploymentOut"];
export type CreateDeploymentRequest = Schemas["CreateDeploymentRequest"];
/** As the API returns it (every field present); requests accept a partial one. */
export type PolicyConfig = Schemas["PolicyConfig-Output"];

// --- Timeline / policy --------------------------------------------------------------

export type PolicyEvaluationResult = Schemas["PolicyEvaluationResult"];
export type TimelineEventItem = Schemas["TimelineEventItem"];
export type TimelinePolicyItem = Schemas["TimelinePolicyItem"];
export type TimelineItem = TimelineEventItem | TimelinePolicyItem;

// --- Metrics ------------------------------------------------------------------------

export type MetricsSummary = Schemas["MetricsSummary"];
export type MetricsOut = Schemas["MetricsOut"];
export type MetricsDeltas = Schemas["MetricsDeltas"];
export type ComparisonOut = Schemas["ComparisonOut"];
export type MetricsBucket = Schemas["MetricsBucket"];
export type MetricsTimeseries = Schemas["MetricsTimeseriesOut"];

// --- Router -------------------------------------------------------------------------

export type ObservedModelConfig = Schemas["ObservedModelConfig"];
export type ObservedRouter = Schemas["ObservedRouterOut"];
export type GatewayView = Schemas["GatewayOut"];
export type GatewayModel = Schemas["GatewayModelOut"];
export type GatewayVersion = Schemas["GatewayVersionOut"];
export type GatewayPolicy = Schemas["GatewayPolicyOut"];

/** One model's slice of the router, in the shape the UI compares against a
 * deployment's desired state (built by observedFor in queries.ts). */
export interface ObservedRouterState {
  reachable: boolean;
  model_name: string | null;
  deployment_id: string | null;
  revision: number | null;
  targets: TargetWeight[] | null;
}

// --- Fleet --------------------------------------------------------------------------

export type Outcome = keyof Schemas["OutcomeCounts"];
export type OutcomeCount = Schemas["OutcomeCount"];
export type FleetSummary = Schemas["FleetSummary"];

// --- Benchmarks ---------------------------------------------------------------------

export type ScenarioInfo = Schemas["ScenarioInfoOut"];
export type BenchmarkRunStatus = Schemas["BenchmarkRunStatus"];
export type LoadTestResult = Schemas["LoadTestResultOut"];
export type BenchmarkResult = Schemas["BenchmarkResultOut"];
export type BenchmarkRun = Schemas["BenchmarkRunOut"];
export type RunBenchmarkRequest = Schemas["RunBenchmarkRequest"];

// --- Not in the OpenAPI schema ------------------------------------------------------

// The local model registry's metadata.json / evaluation.json are free-form JSON
// (see backend/scripts/train_models.py), served as-is - these list the fields the
// dashboard actually reads, plus an index signature for the rest.
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

/** One `change` event on GET /api/stream (Server-Sent Events aren't described by
 * OpenAPI). Mirrors backend/app/control_plane/feed.py's poll_changes. */
export type StreamChange =
  | {
      kind: "event";
      deployment_id: string;
      event_type: string;
      message: string;
      actor: string | null;
      at: string;
    }
  | {
      kind: "policy";
      deployment_id: string;
      policy_name: string;
      result: PolicyEvaluationResult;
      at: string;
    };
