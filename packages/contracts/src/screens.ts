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
  readonly matchCount: number;
  readonly status: "complete" | "failed";
}

export interface ScreenMatch {
  readonly instrumentId: string;
  readonly rank: number;
  readonly score: number | null;
}
