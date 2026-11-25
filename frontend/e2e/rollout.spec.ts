import { expect, test } from "@playwright/test";
import { api, createPausedRollout, E2E_MODEL, rollBackLeftovers } from "./api";

test.afterAll(async () => {
  await rollBackLeftovers(await api());
});

test("a rollout started elsewhere shows up without a reload (SSE)", async ({ page }) => {
  await page.goto("/deployments");
  await expect(page.locator("header [data-stream]")).toHaveAttribute("data-stream", "open");

  const deployment = await createPausedRollout(await api());
  // Polling backs off to 30s while the stream is open, so the new row showing up
  // within a few seconds is the stream's doing. Earlier e2e rollouts may still be
  // listed (rolled back), so match the live one only.
  const row = page.getByRole("row").filter({ hasText: E2E_MODEL }).filter({ hasText: "Canary running" });
  await expect(row).toHaveCount(1, { timeout: 4_000 });

  await row.click();
  const inspector = page.getByRole("dialog");
  await expect(inspector).toBeVisible();
  await inspector.getByRole("link", { name: /open rollout/i }).click();
  await expect(page).toHaveURL(new RegExp(`/deployments/${deployment.id}$`));
});

test("operator rolls back a live canary from the detail page", async ({ page }) => {
  const deployment = await createPausedRollout(await api(), 0.2);
  await page.goto(`/deployments/${deployment.id}`);

  await expect(page.getByText("Canary running").first()).toBeVisible();
  await expect(page.getByText("Paused", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("tab", { name: /Timeline/ })).toBeVisible();

  await page.getByRole("button", { name: "Rollback" }).click();
  const confirm = page.getByRole("alertdialog");
  await expect(confirm.getByText("Roll back to stable?")).toBeVisible();
  await confirm.getByRole("button", { name: "Roll back" }).click();

  await expect(page.getByText("Rolled back").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Rollback" })).toHaveCount(0);

  await page.getByRole("tab", { name: /Timeline/ }).click();
  await expect(page.getByText(/canary receiving traffic at 20%/)).toBeVisible();
  await expect(page.getByText(/rolled back to stable \(manual\)/)).toBeVisible();
});

test("new rollout form refuses identical versions", async ({ page }) => {
  await page.goto("/models");
  const canary = page.getByLabel("Canary version");
  await expect(canary).toBeEnabled();
  const stableValue = (await page.getByLabel("Stable version").innerText()).trim();
  await canary.click();
  await page.getByRole("option", { name: stableValue, exact: true }).click();
  await expect(page.getByText("Stable and canary versions must differ.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start canary rollout" })).toBeDisabled();
});
