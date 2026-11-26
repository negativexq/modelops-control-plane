import { expect, request, test } from "@playwright/test";
import { api, createPausedRollout, E2E_MODEL, rollBackLeftovers } from "./api";

const ROUTER_URL = process.env.E2E_ROUTER_URL ?? "http://localhost:8080";

// One real row from backend/data/test.csv; every version's request model accepts it.
const PAYLOAD = {
  ...Object.fromEntries(
    [
      -1.7054, 0.1433, -1.0706, -1.5247, 0.6394, -0.3766, 2.5314, -1.4381, -0.1816, 2.3891, -0.6455, -0.6487, -0.3678,
      -0.8547, 0.8791, -1.8719, -0.2255, 1.4109, -0.8097, 0.4269,
    ].map((v, i) => [`feature_${i}`, v]),
  ),
  amount: 26.06,
  merchant_category: "grocery",
  transaction_hour: 22,
  device_risk_score: 0.4047,
  customer_age_days: 1443,
  previous_declines: 0,
  country_mismatch: 0,
  is_new_customer: 0,
};

test.afterAll(async () => {
  await rollBackLeftovers(await api());
});

test("the gateway pins a routing key to one version and tags every response", async () => {
  await createPausedRollout(await api(), 0.5);
  const router = await request.newContext({ baseURL: ROUTER_URL });
  const seen = new Set<string>();
  const ids = new Set<string>();
  for (let i = 0; i < 6; i++) {
    const res = await router.post(`/router/models/${E2E_MODEL}/predict`, {
      data: PAYLOAD,
      headers: { "X-Routing-Key": "customer-42" },
    });
    expect(res.status()).toBe(200);
    seen.add(res.headers()["x-routed-to"]);
    ids.add(res.headers()["x-request-id"]);
  }
  // At a 50/50 split, six unpinned requests landing on one version would be a 1-in-32 fluke.
  expect(seen.size).toBe(1);
  expect(["v1", "v2-good"]).toContain([...seen][0]);
  expect(ids.size).toBe(6);
});

test("the gateway page draws a live rollout's flow and control loop", async ({ page }) => {
  await createPausedRollout(await api(), 0.15);
  await page.goto("/gateway");
  await expect(page.getByRole("heading", { name: "Gateway" })).toBeVisible();

  const flow = page.getByTestId(`gateway-flow-${E2E_MODEL}`);
  await expect(flow).toBeVisible();
  // Weights are the split the router applied, so this also proves the push landed.
  await expect(flow.getByTestId("gateway-version-v1")).toContainText("85%");
  await expect(flow.getByTestId("gateway-version-v2-good")).toContainText("15%");
  await expect(flow.getByTestId("gateway-version-v2-good")).toContainText("canary");
  await expect(flow.getByTestId("gateway-version-v1")).toContainText("closed");

  const card = page.locator('[data-slot="card"]').filter({ has: flow });
  await expect(card.getByTestId("control-loop")).toBeVisible();
  await expect(card.getByTestId("control-loop")).toContainText("Policy engine");
});

test("sidebar reaches the gateway page", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: "Gateway", exact: true }).first().click();
  await expect(page).toHaveURL(/\/gateway$/);
  await expect(page.getByRole("heading", { name: "Gateway" })).toBeVisible();
});
