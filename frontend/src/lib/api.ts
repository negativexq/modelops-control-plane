import type {
  BenchmarkRun,
  ComparisonOut,
  CreateDeploymentRequest,
  DeploymentOut,
  MetricsOut,
  ModelVersionEvaluation,
  ModelVersionMetadata,
  RunBenchmarkRequest,
  ScenarioInfo,
  TimelineItem,
} from "./types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: {
        "content-type": "application/json",
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(0, `Could not reach the control plane at ${API_BASE_URL}`);
  }

  if (!response.ok) {
    const detail = await response
      .json()
      .then((body: { detail?: unknown }) =>
        typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail),
      )
      .catch(() => response.statusText);
    throw new ApiError(response.status, detail || `Request failed with ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

// --- Model registry -------------------------------------------------------

export function getModels(): Promise<string[]> {
  return request<string[]>("/api/models");
}

export function getModelVersions(modelName: string): Promise<string[]> {
  return request<string[]>(`/api/models/${encodeURIComponent(modelName)}/versions`);
}

export function getModelVersionMetadata(
  modelName: string,
  version: string,
): Promise<ModelVersionMetadata> {
  return request<ModelVersionMetadata>(
    `/api/models/${encodeURIComponent(modelName)}/versions/${encodeURIComponent(version)}`,
  );
}

export function getModelVersionEvaluation(
  modelName: string,
  version: string,
): Promise<ModelVersionEvaluation> {
  return request<ModelVersionEvaluation>(
    `/api/models/${encodeURIComponent(modelName)}/versions/${encodeURIComponent(version)}/evaluation`,
  );
}

// --- Deployments -----------------------------------------------------------

export function listDeployments(): Promise<DeploymentOut[]> {
  return request<DeploymentOut[]>("/api/deployments");
}

export function getDeployment(id: string): Promise<DeploymentOut> {
  return request<DeploymentOut>(`/api/deployments/${encodeURIComponent(id)}`);
}

export function createDeployment(
  payload: CreateDeploymentRequest,
  idempotencyKey?: string,
): Promise<DeploymentOut> {
  return request<DeploymentOut>("/api/deployments", {
    method: "POST",
    body: JSON.stringify(payload),
    headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
  });
}

export function promoteDeployment(id: string): Promise<DeploymentOut> {
  return request<DeploymentOut>(`/api/deployments/${encodeURIComponent(id)}/promote`, {
    method: "POST",
  });
}

export function rollbackDeployment(id: string): Promise<DeploymentOut> {
  return request<DeploymentOut>(`/api/deployments/${encodeURIComponent(id)}/rollback`, {
    method: "POST",
  });
}

export function getDeploymentMetrics(
  id: string,
  windowSeconds?: number,
): Promise<MetricsOut> {
  const query = windowSeconds ? `?window_seconds=${windowSeconds}` : "";
  return request<MetricsOut>(`/api/deployments/${encodeURIComponent(id)}/metrics${query}`);
}

export function getDeploymentComparison(
  id: string,
  windowSeconds?: number,
): Promise<ComparisonOut> {
  const query = windowSeconds ? `?window_seconds=${windowSeconds}` : "";
  return request<ComparisonOut>(`/api/deployments/${encodeURIComponent(id)}/comparison${query}`);
}

export function getDeploymentTimeline(id: string): Promise<TimelineItem[]> {
  return request<TimelineItem[]>(`/api/deployments/${encodeURIComponent(id)}/timeline`);
}

// --- Benchmarks --------------------------------------------------------------

export function getScenarios(): Promise<ScenarioInfo[]> {
  return request<ScenarioInfo[]>("/api/benchmarks/scenarios");
}

export function getCurrentBenchmarkRun(): Promise<BenchmarkRun | null> {
  return request<BenchmarkRun | null>("/api/benchmarks/current");
}

export function startBenchmarkRun(payload: RunBenchmarkRequest): Promise<BenchmarkRun> {
  return request<BenchmarkRun>("/api/benchmarks/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getBenchmarkRun(id: string): Promise<BenchmarkRun> {
  return request<BenchmarkRun>(`/api/benchmarks/${encodeURIComponent(id)}`);
}

export function listBenchmarkRuns(): Promise<BenchmarkRun[]> {
  return request<BenchmarkRun[]>("/api/benchmarks");
}
