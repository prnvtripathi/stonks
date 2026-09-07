import { describe, expect, it } from "vitest";
import { DEFAULT_GLOSSARY_ENTRIES, parseGlossaryEntry, validateGlossaryEntries, type GlossaryEntry } from "./glossary";

const ENTRY: GlossaryEntry = {
  slug: "example-term", term: "Example term", aliases: ["Example"], summary: "A concise explanation.",
  interpretation: "Use it as context, not as a decision rule.", pitfalls: ["It can be misleading without context."], assetClasses: ["equity"],
  sources: [{ label: "SEBI Investor", url: "https://investor.sebi.gov.in/" }], reviewedAt: "2026-09-07",
};

describe("curated glossary contract", () => {
  it("requires an authoritative source and review date", () => {
    expect(() => parseGlossaryEntry({ ...ENTRY, sources: [], reviewedAt: undefined })).toThrow();
  });
  it("rejects insecure sources and formulas without provenance", () => {
    expect(() => parseGlossaryEntry({ ...ENTRY, sources: [{ label: "Source", url: "http://example.com" }] })).toThrow(/https/i);
    expect(() => parseGlossaryEntry({ ...ENTRY, formula: { expression: "a / b" } })).toThrow(/provenance/i);
  });
  it("accepts root-relative links for app-owned methodology", () => {
    expect(parseGlossaryEntry({ ...ENTRY, formula: { expression: "a / b", provenance: [{ label: "Stonks methodology", url: "/learn/example-term" }] } }).formula?.provenance[0]?.url).toBe("/learn/example-term");
  });

  it("rejects fields outside the reviewed entry shape", () => {
    expect(() => parseGlossaryEntry({ ...ENTRY, unexpected: true })).toThrow(/unknown|additional/i);
    expect(() => parseGlossaryEntry({ ...ENTRY, sources: [{ label: "Source", url: "https://" }] })).toThrow(/url/i);
  });
  it("rejects duplicate slugs and terms", () => {
    expect(() => validateGlossaryEntries([ENTRY, { ...ENTRY, term: "Another term" }])).toThrow(/duplicate/i);
    expect(() => validateGlossaryEntries([ENTRY, { ...ENTRY, slug: "other-term" }])).toThrow(/duplicate/i);
  });
  it("ships every required financial concept as reviewed content", () => {
    expect(DEFAULT_GLOSSARY_ENTRIES.length).toBeGreaterThanOrEqual(16);
    expect(DEFAULT_GLOSSARY_ENTRIES.every((entry) => entry.sources.length > 0 && entry.reviewedAt)).toBe(true);
    expect(DEFAULT_GLOSSARY_ENTRIES.map((entry) => entry.slug)).toEqual(expect.arrayContaining([
      "return", "volume", "average-volume", "market-cap", "moving-average", "52-week-high", "relative-strength-benchmark", "relative-strength-rating", "volatility", "drawdown", "nav", "roe", "roce", "debt-equity", "pe", "cash-flow",
    ]));
  });
});
