import type { AssetClass } from "./metrics";

export interface SavedScreen {
  readonly id: string;
  readonly name: string;
  readonly source: string;
  readonly languageVersion: string;
  readonly createdAt: string;
  readonly updatedAt: string;
}

export interface ScreenRun {
  readonly id: string;
  readonly screenId: string;
  readonly datasetId: string;
  readonly effectiveDate: string;
  readonly completedAt?: string;
  readonly matchCount: number;
  readonly status: "complete" | "failed";
  /** Null for runs written before immutable query snapshots were introduced. */
  readonly source: string | null;
  /** Null for runs written before immutable query snapshots were introduced. */
  readonly languageVersion: string | null;
}

export interface ScreenMatch {
  readonly instrumentId: string;
  readonly rank: number;
  readonly score: number | null;
  /** Null for matches written before immutable identity snapshots were introduced. */
  readonly symbol?: string | null;
  readonly name?: string | null;
  readonly assetClass?: AssetClass | null;
}
