import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ComparisonTable, InstrumentResearch, MetricValue, MomentumBreakdown, ResultsTable } from "./research";
import type { InstrumentDto, ResultMatchDto } from "./api";

const equity: InstrumentDto = {
  instrumentId: "infy",
  symbol: "INFY",
  name: "Infosys Limited",
  assetClass: "equity",
  active: true,
  metricRows: [
    { metric: "return_12m", value: 24.5, state: "present", effectiveDate: "2026-09-04" },
    { metric: "roe", value: null, state: "missing", effectiveDate: "2026-09-04" },
    { metric: "category_rank", value: null, state: "not_applicable", effectiveDate: "2026-09-04" },
  ],
};
const other: InstrumentDto = { ...equity, instrumentId: "tcs", symbol: "TCS", name: "Tata Consultancy Services" };
const match: ResultMatchDto = {
  instrumentId: "infy",
  rank: 1,
  score: 87,
  entered: true,
  exited: false,
  explanation: {
    matched: true,
    text: "Return over 12months is 24.5%, above 10%.",
    metrics: ["return_12m"],
    clauses: [{ clause: "Return over 12months > 10%", result: "Matched", value: { value: 24.5, state: "present" } }],
  },
  momentum: {
    components: [{ label: "12-month relative strength", raw: 24.5, normalized: 82, weight: 0.35, contribution: 28.7 }],
    cohort: "NSE EQ ordinary shares",
    formulaVersion: "momentum-v1",
    coverage: 1,
    sourceDate: "2026-09-04",
  },
};

describe("Task 11 research views", () => {
  it("distinguishes present, missing, and not applicable values", () => {
    render(<div><MetricValue value={{ value: 24.5, state: "present" }} unit="percent" /><MetricValue value={{ value: null, state: "missing" }} /><MetricValue value={{ value: null, state: "not_applicable" }} /></div>);
    expect(screen.getByText("24.5%")) .toBeVisible();
    expect(screen.getByText("Missing data")).toBeVisible();
    expect(screen.getByText("Not applicable")).toBeVisible();
  });

  it("shows every momentum component and coverage context", () => {
    render(<MomentumBreakdown momentum={match.momentum!} />);
    expect(screen.getByRole("heading", { name: /momentum breakdown/i })).toBeVisible();
    expect(screen.getByText("Raw value")).toBeVisible();
    expect(screen.getByText("NSE EQ ordinary shares")).toBeVisible();
    expect(screen.getByText(/momentum-v1/)).toBeVisible();
    expect(screen.getByText(/100% coverage/)).toBeVisible();
  });

  it("sorts and paginates labeled result rows", async () => {
    const onPage = vi.fn();
    render(<ResultsTable matches={[match, { ...match, instrumentId: "tcs", rank: 2, score: 72 }]} instruments={[equity, other]} limit={1} offset={0} total={2} onPage={onPage} />);
    const table = screen.getByRole("table", { name: /screen results/i });
    expect(within(table).getByText("INFY")).toBeVisible();
    expect(screen.getByRole("button", { name: /next page/i })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /next page/i }));
    expect(onPage).toHaveBeenCalledWith(1);
    fireEvent.click(screen.getByRole("button", { name: /sort by score/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /sort by score/i })).toHaveAttribute("aria-sort", "descending"));
  });

  it("labels incomparable asset metrics in comparison", () => {
    render(<ComparisonTable instruments={[equity, { ...other, assetClass: "mutual_fund", metricRows: [{ metric: "return_12m", value: 12, state: "present" }] }]} />);
    expect(screen.getByText(/not directly comparable/i)).toBeVisible();
    expect(screen.getByText("Asset class")).toBeVisible();
  });

  it("renders an asset-specific instrument research heading", () => {
    render(<InstrumentResearch instrument={equity} chart={{ points: [{ date: "2026-01-01", value: 100 }, { date: "2026-02-01", value: 110 }] }} />);
    expect(screen.getByRole("heading", { name: /infosys limited research/i })).toBeVisible();
    expect(screen.getByRole("heading", { name: /adjusted price history/i })).toBeVisible();
    expect(screen.getByRole("table", { name: /history/i })).toBeVisible();
  });
});
