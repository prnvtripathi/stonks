import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App, type DashboardApi } from "./app";
import type { MetricDefinition, SavedScreen } from "@stonks/contracts";
import type { StatusDto } from "./api";

const metrics: MetricDefinition[] = [{ id: "volume", label: "Volume", aliases: ["Volume"], column: "volume", valueType: "number", unit: "count", assetClasses: ["equity", "etf"] }];
const status: StatusDto = {
  effectiveDate: "2026-09-04",
  datasetId: "dataset-a",
  sources: [
    { sourceId: "NSE EOD", expectedDate: "2026-09-04", loadedDate: "2026-09-04", status: "complete", stale: false },
    { sourceId: "AMFI NAV", expectedDate: "2026-09-04", loadedDate: "2026-09-03", status: "delayed", stale: true },
  ],
};
const screens: SavedScreen[] = [{ id: "screen-1", name: "Momentum watch", source: "Volume > 500000", languageVersion: "v1", createdAt: "2026-09-01", updatedAt: "2026-09-04" }];

function fakeApi(): DashboardApi {
  return {
    getStatus: vi.fn(async () => status),
    getMetrics: vi.fn(async () => metrics),
    getScreens: vi.fn(async () => ({ data: screens, pagination: { limit: 50, offset: 0, total: 1 } })),
    createScreen: vi.fn(async (input: { readonly name: string; readonly source: string }) => ({ ...screens[0]!, ...input, id: "screen-new" })),
    runScreen: vi.fn(async () => ({ id: "run-1", screenId: "screen-1", datasetId: "dataset-a", effectiveDate: "2026-09-04", matchCount: 3, status: "complete" as const, matches: [] })),
  };
}

describe("dashboard shell", () => {
  it("shows independent source dates and persistent disclosure", async () => {
    render(<App api={fakeApi()} />);
    expect(await screen.findByText("AMFI NAV delayed")).toBeVisible();
    const datasetContext = screen.getByRole("region", { name: "Dataset context" });
    expect(within(datasetContext).getByText("Data through")).toBeVisible();
    expect(within(datasetContext).getByText("04 Sep 2026")).toBeVisible();
    expect(screen.getByText(/not investment advice/i)).toBeVisible();
  });

  it("supports skip link, keyboard navigation, and screen editor autocomplete", async () => {
    render(<App api={fakeApi()} />);
    const skip = screen.getByRole("link", { name: /skip to main/i });
    expect(skip).toHaveAttribute("href", "#main-content");
    fireEvent.click(screen.getByRole("button", { name: /new screen/i }));
    const editor = screen.getByRole("textbox", { name: /screen query/i });
    fireEvent.change(editor, { target: { value: "Volum" } });
    expect(await screen.findByRole("option", { name: "Volume" })).toBeVisible();
    fireEvent.keyDown(editor, { key: "ArrowDown" });
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(editor).toHaveValue("Volume");
  });

  it("warns before leaving an edited screen and validates after debounce", async () => {
    const api = fakeApi();
    render(<App api={api} />);
    fireEvent.click(screen.getByRole("button", { name: /new screen/i }));
    fireEvent.change(screen.getByRole("textbox", { name: /screen name/i }), { target: { value: "Volume pulse" } });
    fireEvent.change(screen.getByRole("textbox", { name: /screen query/i }), { target: { value: "Volume > 500000" } });
    await waitFor(() => expect(screen.getByText(/Query looks valid/i)).toBeVisible());
    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary navigation" })).getByRole("button", { name: "Overview" }));
    expect(screen.getByRole("dialog")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /stay/i }));
    expect(screen.getByRole("textbox", { name: /screen query/i })).toBeVisible();
  });
});
