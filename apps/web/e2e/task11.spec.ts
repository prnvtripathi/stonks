import { expect, test } from "@playwright/test";

test("explains a persisted match, preserves back navigation, and sends server sort requests", async ({ page }) => {
  const resultRequests: string[] = [];
  const screen = { id: "volume-breakout", name: "Volume breakout", source: "Volume > 500000", languageVersion: "v1", createdAt: "2026-09-01", updatedAt: "2026-09-04" };
  const momentum = { components: [{ componentId: "six_month_performance", label: "Six-month performance", unit: "percent", raw: 0.24, normalized: 0.82, weight: 0.2, contribution: 0.164 }], cohort: "equity", formulaVersion: "momentum-v2-cohort", coverage: 1, sourceDate: "2026-09-04" };
  const match = { instrumentId: "INFY", symbol: "INFY", rank: 1, score: 0.87, entered: true, exited: false, explanation: { matched: true, text: "Volume matched", metrics: ["volume"], clauses: [{ clause: "Volume > 500000", metric: "volume", result: "Matched" as const, value: { value: 900000, state: "present" as const } }] }, momentum };
  await page.route("**/api/v1/status", (route) => route.fulfill({ json: { effectiveDate: "2026-09-04", datasetId: "dataset-1", sources: [] } }));
  await page.route("**/api/v1/metrics", (route) => route.fulfill({ json: { metrics: [] } }));
  await page.route("**/api/v1/screens", (route) => route.fulfill({ json: { data: [screen], pagination: { limit: 50, offset: 0, total: 1 } } }));
  await page.route("**/api/v1/screens/volume-breakout/runs**", (route) => route.fulfill({ json: { screen, runs: [{ id: "run-1", screenId: screen.id, datasetId: "dataset-1", effectiveDate: "2026-09-04", completedAt: "2026-09-04T10:00:00Z", matchCount: 1, status: "complete", source: screen.source, languageVersion: "v1", matches: [{ instrumentId: "INFY", entered: true, exited: false }] }] } }));
  await page.route("**/api/v1/screens/volume-breakout/results**", (route) => { resultRequests.push(route.request().url()); return route.fulfill({ json: { screen, run: { id: "run-1", screenId: screen.id, datasetId: "dataset-1", effectiveDate: "2026-09-04", completedAt: "2026-09-04T10:00:00Z", matchCount: 1, status: "complete", source: screen.source, languageVersion: "v1", isCurrentDataset: true, isCurrentQuery: true, matches: [match] }, pagination: { limit: 25, offset: 0, total: 1 } } }); });
  await page.route("**/api/v1/instruments/INFY", (route) => route.fulfill({ json: { instrumentId: "INFY", symbol: "INFY", name: "Infosys Limited", assetClass: "equity", active: true, metricRows: [{ metric: "momentum_score", value: 0.87, state: "present", effectiveDate: "2026-09-04", normalizedValue: 0.87, formulaVersion: "momentum-v2-cohort" }], momentum, fundamentalPeriods: [{ periodId: "p1", periodEnd: "2026-06-30", periodType: "quarter", filingId: "f1", filedAt: "2026-08-01", metrics: { revenue: 100 } }], corporateActions: [{ actionId: "a1", actionDate: "2026-07-15", actionType: "dividend", numerator: 10, denominator: 1 }] } }));
  await page.route("**/api/v1/instruments/INFY/chart", (route) => route.fulfill({ json: { points: [{ date: "2026-01-01", value: 100 }, { date: "2026-09-04", value: 120 }], valueLabel: "Adjusted price" } }));
  await page.goto("/screens/volume-breakout");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Run screen" }).click();
  await page.getByRole("link", { name: /view first match/i }).click();
  await expect(page.getByRole("heading", { name: /momentum breakdown/i })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Fundamental periods" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Corporate actions" })).toBeVisible();
  await page.getByRole("button", { name: /results/i }).click();
  await expect(page.getByRole("heading", { name: /volume breakout results/i })).toBeVisible();
  await page.getByRole("button", { name: /sort by score/i }).click();
  await expect.poll(() => resultRequests.at(-1) ?? "").toContain("sort=score");
  await expect.poll(() => resultRequests.at(-1) ?? "").toContain("direction=desc");
});
