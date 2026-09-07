import { expect, test } from "@playwright/test";

test("screens work at 320px", async ({ page }) => {
  const screen = { id: "volume-breakout", name: "Volume breakout", source: "Volume > 500000", languageVersion: "v1", createdAt: "2026-09-01", updatedAt: "2026-09-04" };
  await page.route("**/api/v1/status", (route) => route.fulfill({ json: { effectiveDate: "2026-09-04", datasetId: "dataset-1", sources: [] } }));
  await page.route("**/api/v1/metrics", (route) => route.fulfill({ json: { metrics: [] } }));
  await page.route("**/api/v1/screens", (route) => route.fulfill({ json: { data: [screen], pagination: { limit: 50, offset: 0, total: 1 } } }));
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/screens");
  await expect(page.getByRole("heading", { name: "Saved screens" })).toBeVisible();
});
