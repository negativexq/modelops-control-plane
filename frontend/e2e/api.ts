import { request, type APIRequestContext } from "@playwright/test";

export const API_URL = process.env.E2E_API_URL ?? "http://localhost:8000";

/** Its own model name, so these tests never collide with a real fraud-model
 * rollout (one active rollout per model is enforced by the control plane). */
export const E2E_MODEL = "e2e-ui";

const ACTIVE = new Set([
  "PENDING",
  "DEPLOYING",
  "CANARY_RUNNING",
  "EVALUATING",
  "PROMOTING",
  "ROLLING_BACK",
  "INCONCLUSIVE",
]);

interface Deployment {
  id: string;
  model_name: string;
  status: string;
}

export async function api(): Promise<APIRequestContext> {
  return request.newContext({ baseURL: API_URL });
}

export async function rollBackLeftovers(ctx: APIRequestContext) {
  const res = await ctx.get("/api/deployments");
  const all = (await res.json()) as Deployment[];
  for (const d of all.filter((d) => d.model_name === E2E_MODEL && ACTIVE.has(d.status))) {
    await ctx.post(`/api/deployments/${d.id}/rollback`);
  }
}

/** A paused rollout: the worker won't promote or roll it back underneath the test. */
export async function createPausedRollout(ctx: APIRequestContext, canaryWeight = 0.15): Promise<Deployment> {
  await rollBackLeftovers(ctx);
  const res = await ctx.post("/api/deployments", {
    data: {
      model_name: E2E_MODEL,
      stable_version: "v1",
      canary_version: "v2-good",
      canary_weight: canaryWeight,
      automation_paused: true,
    },
  });
  if (!res.ok()) throw new Error(`create failed: ${res.status()} ${await res.text()}`);
  return (await res.json()) as Deployment;
}
