import { expect, test } from "@playwright/test";

test("explains a saved-screen match and opens research", async ({ page }) => {
  await page.route("**/api/v1/status", (route) => route.fulfill({ json: { effectiveDate: "2026-09-04", datasetId: "dataset-1", sources: [] } }));
  await page.route("**/api/v1/metrics", (route) => route.fulfill({ json: { metrics: [] } }));
  await page.route("**/api/v1/screens", (route) => route.fulfill({ json: { data: [{ id: "volume-breakout", name: "Volume breakout", source: "Volume > 500000", languageVersion: "v1", createdAt: "2026-09-01", updatedAt: "2026-09-04" }], pagination: { limit: 50, offset: 0, total: 1 } } }));
  await page.route("**/api/v1/screens/volume-breakout/runs", (route) => route.fulfill({ json: { screen: { id: "volume-breakout", name: "Volume breakout", source: "Volume > 500000", languageVersion: "v1", createdAt: "2026-09-01", updatedAt: "2026-09-04" }, runs: [{ id: "run-1", screenId: "volume-breakout", datasetId: "dataset-1", effectiveDate: "2026-09-04", matchCount: 1, status: "complete", matches: [{ instrumentId: "INFY", entered: true, exited: false }] }] } }));
  await page.route("**/api/v1/screens/volume-breakout/results**", (route) => route.fulfill({ json: { screen: { id: "volume-breakout", name: "Volume breakout", source: "Volume > 500000", languageVersion: "v1", createdAt: "2026-09-01", updatedAt: "2026-09-04" }, run: { id: "run-1", screenId: "volume-breakout", datasetId: "dataset-1", effectiveDate: "2026-09-04", matchCount: 1, status: "complete", matches: [{ instrumentId: "INFY", symbol: "INFY", rank: 1, score: 87, entered: true, exited: false, explanation: { matched: true, text: "Volume matched", metrics: ["volume"], clauses: [{ clause: "Volume > 500000", result: "Matched", value: { value: 900000, state: "present" } }] }, momentum: { components: [], cohort: "NSE EQ ordinary shares", formulaVersion: "momentum-v1", coverage: 1, sourceDate: "2026-09-04" } }] }, pagination: { limit: 25, offset: 0, total: 1 } } }));
  await page.route("**/api/v1/instruments/INFY", (route) => route.fulfill({ json: { instrumentId: "INFY", symbol: "INFY", name: "Infosys Limited", assetClass: "equity", active: true, metricRows: [{ metric: "momentum_score", value: 87, state: "present", effectiveDate: "2026-09-04" }], momentum: { components: [], cohort: "NSE EQ ordinary shares", formulaVersion: "momentum-v1", coverage: 1, sourceDate: "2026-09-04" } } }));
  await page.route("**/api/v1/instruments/INFY/chart", (route) => route.fulfill({ json: { points: [{ date: "2026-01-01", value: 100 }, { date: "2026-09-04", value: 120 }], valueLabel: "Adjusted price" } }));
  await page.goto("/screens/volume-breakout");
  await page.getByRole("button", { name: "Run screen" }).click();
  await page.getByRole("link", { name: /view first match/i }).click();
  await expect(page.getByRole("heading", { name: /momentum breakdown/i })).toBeVisible();
});
