import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LearnView, TermPopover } from "./learn";
import type { GlossaryEntry } from "@stonks/contracts";

const entry: GlossaryEntry = { slug: "volume", term: "Volume", aliases: ["Traded volume"], summary: "The number of units traded.", interpretation: "Compare with its own history and asset class.", pitfalls: ["A single day can be noisy."], assetClasses: ["equity", "etf"], sources: [{ label: "NSE India", url: "https://www.nseindia.com/" }], reviewedAt: "2026-09-07" };
describe("Learn views", () => {
  it("loads glossary content through the API client when no entries are supplied", async () => {
    const getGlossary = vi.fn(async () => ({ data: [entry], pagination: { limit: 50, offset: 0, total: 1 } }));
    render(<LearnView api={{ getGlossary }} />);
    await waitFor(() => expect(getGlossary).toHaveBeenCalled());
    expect(await screen.findByText("Volume")).toBeVisible();
  });
  it("searches terms and exposes full entry details", () => {
    render(<LearnView entries={[entry, { ...entry, slug: "nav", term: "NAV" }]} />); fireEvent.change(screen.getByRole("searchbox", { name: /search learn/i }), { target: { value: "vol" } }); fireEvent.click(screen.getByRole("button", { name: /volume/i }));
    expect(screen.getByRole("heading", { name: /volume/i })).toBeVisible(); expect(screen.getByText(/reviewed 07 sep 2026/i)).toBeVisible(); expect(screen.getByRole("link", { name: /nse india/i })).toHaveAttribute("target", "_blank");
  });
  it("opens a contextual popover by keyboard, closes on Escape and outside click", () => {
    const onLearn = vi.fn(); render(<TermPopover entry={entry} onLearn={onLearn}>Volume</TermPopover>); const trigger = screen.getByRole("button", { name: /learn about volume/i });
    fireEvent.keyDown(trigger, { key: "Enter" }); expect(screen.getByRole("dialog", { name: /volume/i })).toBeVisible(); expect(screen.getByRole("link", { name: /full volume entry/i })).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" }); expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); fireEvent.click(trigger); fireEvent.mouseDown(document.body); expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); fireEvent.click(trigger); fireEvent.click(screen.getByRole("link", { name: /full volume entry/i })); expect(onLearn).toHaveBeenCalledWith("volume");
  });
});
