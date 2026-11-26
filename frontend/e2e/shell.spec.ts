import { expect, test } from "@playwright/test";

test("overview renders fleet KPIs and the live stream connects", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  for (const label of ["Live rollouts", "Promoted", "Rolled back", "Inconclusive"]) {
    await expect(page.getByText(label, { exact: true }).first()).toBeVisible();
  }
  // Every model the router serves, each with its split, at once.
  await expect(page.getByText(/What the router is serving right now, for every model/)).toBeVisible();
  const models = page.getByTestId("fleet-models").locator("li[data-model]");
  await expect(models.first()).toBeVisible();
  await expect(models.first()).toContainText("%");
  // The header indicator switches from "Polling" to "Live" once /api/stream is open.
  await expect(page.locator("header [data-stream]")).toHaveAttribute("data-stream", "open");
});

test("command palette navigates between screens", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByRole("dialog");
  await expect(palette).toBeVisible();
  await palette.getByRole("combobox").fill("bench");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/benchmarks$/);
  await expect(page.getByRole("heading", { name: "Benchmarks" })).toBeVisible();
});

test("sidebar links reach every screen", async ({ page }) => {
  await page.goto("/");
  for (const [name, path] of [
    ["Deployments", "/deployments"],
    ["Models", "/models"],
    ["Benchmarks", "/benchmarks"],
    ["Overview", "/"],
  ] as const) {
    await page.getByRole("link", { name, exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(`${path === "/" ? "/$" : path}`));
  }
});
