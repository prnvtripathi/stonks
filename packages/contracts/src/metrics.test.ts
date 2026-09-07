import { expect, it } from "vitest";
import { DEFAULT_METRIC_CATALOG } from "./metrics";

it("publishes the screener metric aliases", () => {
  expect(DEFAULT_METRIC_CATALOG.find((metric) => metric.id === "volume_1w_avg")?.aliases).toContain(
    "Volume 1week average",
  );
});
