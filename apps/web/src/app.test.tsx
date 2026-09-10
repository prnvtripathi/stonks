import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App, ScreenEditor, type DashboardApi } from "./app";
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
    runScreen: vi.fn(async () => ({ id: "run-1", screenId: "screen-1", datasetId: "dataset-a", effectiveDate: "2026-09-04", matchCount: 3, status: "complete" as const, source: "Volume > 500000", languageVersion: "v1", matches: [] })),
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

  it("replaces only the current metric fragment in a full query", async () => {
    render(<ScreenEditor initialValue="Volume > 500000 AND Volum" metrics={metrics} />);
    const editor = screen.getByRole("textbox", { name: /screen query/i });
    expect(await screen.findByRole("option", { name: "Volume" })).toBeVisible();
    fireEvent.keyDown(editor, { key: "Enter" });
    expect(editor).toHaveValue("Volume > 500000 AND Volume");
  });

  it("uses the update API for edit mode without creating a new screen", async () => {
    const api = fakeApi();
    api.updateScreen = vi.fn(async (id, input) => ({ ...screens[0]!, ...input, id, updatedAt: "2026-09-05" }));
    render(<ScreenEditor initialScreen={screens[0]!} mode="edit" metrics={metrics} api={api} />);
    fireEvent.change(screen.getByRole("textbox", { name: /screen name/i }), { target: { value: "Momentum revised" } });
    fireEvent.change(screen.getByRole("textbox", { name: /screen query/i }), { target: { value: "Volume > 1000" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(api.updateScreen).toHaveBeenCalledWith("screen-1", { name: "Momentum revised", source: "Volume > 1000" }));
    expect(api.createScreen).not.toHaveBeenCalled();
  });

  it("protects dirty drafts from beforeunload and traps the unsaved dialog", async () => {
    render(<App api={fakeApi()} />);
    fireEvent.click(screen.getByRole("button", { name: /new screen/i }));
    fireEvent.change(screen.getByRole("textbox", { name: /screen name/i }), { target: { value: "Draft" } });
    const overview = within(screen.getByRole("navigation", { name: "Primary navigation" })).getByRole("button", { name: "Overview" });
    fireEvent.click(overview);
    const dialog = screen.getByRole("dialog");
    expect(screen.getByRole("button", { name: "Stay" })).toHaveFocus();
    expect(document.getElementById("main-content")).toHaveAttribute("aria-hidden", "true");
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /screen name/i })).toHaveValue("Draft");
  });

  it("surfaces initial-load and run failures with retry actions", async () => {
    const api = fakeApi();
    api.getStatus = vi.fn(async () => { throw new Error("offline"); });
    render(<App api={api} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/unable to load/i);
    expect(screen.getByRole("button", { name: /retry/i })).toBeVisible();
  });

  it("keeps the default API client stable across state renders", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), "https://dashboard.example.com").pathname;
      if (path.endsWith("/status")) return Response.json(status);
      if (path.endsWith("/metrics")) return Response.json({ metrics });
      return Response.json({ data: [], pagination: { limit: 50, offset: 0, total: 0 } });
    });
    vi.stubGlobal("fetch", fetchMock);
    try {
      render(<App />);
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
      const paths = fetchMock.mock.calls.map(([input]) => new URL(String(input), "https://dashboard.example.com").pathname);
      expect(paths.filter((path) => path.endsWith("/status"))).toHaveLength(1);
      expect(paths.filter((path) => path.endsWith("/metrics"))).toHaveLength(1);
      expect(paths.filter((path) => path.endsWith("/screens"))).toHaveLength(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
