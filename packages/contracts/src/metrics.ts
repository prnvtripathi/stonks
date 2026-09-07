export type AssetClass = "equity" | "etf" | "mutual_fund";
export type MetricState = "present" | "missing" | "not_applicable";
export type MetricValueType = "number";
export type MetricUnit = "number" | "percent" | "currency" | "count" | "ratio";

export interface MetricDefinition {
  readonly id: string;
  readonly label: string;
  readonly aliases: readonly string[];
  readonly column: string;
  readonly valueType: MetricValueType;
  readonly unit: MetricUnit;
  readonly assetClasses: readonly AssetClass[];
}

export type MetricCatalog = readonly MetricDefinition[];

const allClasses: readonly AssetClass[] = ["equity", "etf", "mutual_fund"];
const priceClasses: readonly AssetClass[] = ["equity", "etf"];
const equityClasses: readonly AssetClass[] = ["equity"];

export const DEFAULT_METRIC_CATALOG: MetricCatalog = [
  {
    id: "return_1d",
    label: "Return over 1day",
    aliases: ["Return over 1day", "Return 1day", "1 day return"],
    column: "return_1d",
    valueType: "number",
    unit: "percent",
    assetClasses: priceClasses,
  },
  {
    id: "volume",
    label: "Volume",
    aliases: ["Volume"],
    column: "volume",
    valueType: "number",
    unit: "count",
    assetClasses: priceClasses,
  },
  {
    id: "volume_1w_avg",
    label: "Volume 1week average",
    aliases: ["Volume 1week average", "Volume 1 week average", "Weekly average volume"],
    column: "volume_1w_avg",
    valueType: "number",
    unit: "count",
    assetClasses: priceClasses,
  },
  {
    id: "market_cap",
    label: "Market Capitalization",
    aliases: ["Market Capitalization", "Market Cap", "Market capitalization"],
    column: "market_cap",
    valueType: "number",
    unit: "currency",
    assetClasses: equityClasses,
  },
  {
    id: "return_1w",
    label: "Return over 1week",
    aliases: ["Return over 1week", "Return 1week"],
    column: "return_1w",
    valueType: "number",
    unit: "percent",
    assetClasses: allClasses,
  },
  {
    id: "return_1m",
    label: "Return over 1month",
    aliases: ["Return over 1month", "Return 1month"],
    column: "return_1m",
    valueType: "number",
    unit: "percent",
    assetClasses: allClasses,
  },
  {
    id: "return_3m",
    label: "Return over 3months",
    aliases: ["Return over 3months", "Return 3months"],
    column: "return_3m",
    valueType: "number",
    unit: "percent",
    assetClasses: allClasses,
  },
  {
    id: "return_6m",
    label: "Return over 6months",
    aliases: ["Return over 6months", "Return 6months"],
    column: "return_6m",
    valueType: "number",
    unit: "percent",
    assetClasses: allClasses,
  },
  {
    id: "return_12m",
    label: "Return over 12months",
    aliases: ["Return over 12months", "Return 12months"],
    column: "return_12m",
    valueType: "number",
    unit: "percent",
    assetClasses: allClasses,
  },
  {
    id: "rs_rating",
    label: "RS Rating",
    aliases: ["RS Rating", "Relative Strength Rating"],
    column: "rs_rating",
    valueType: "number",
    unit: "number",
    assetClasses: priceClasses,
  },
  {
    id: "momentum_score",
    label: "Momentum Score",
    aliases: ["Momentum Score", "Momentum"],
    column: "momentum_score",
    valueType: "number",
    unit: "number",
    assetClasses: allClasses,
  },
  {
    id: "volatility_1y",
    label: "Volatility 1year",
    aliases: ["Volatility 1year", "Annualized Volatility"],
    column: "volatility_1y",
    valueType: "number",
    unit: "percent",
    assetClasses: allClasses,
  },
  {
    id: "max_drawdown_1y",
    label: "Maximum Drawdown 1year",
    aliases: ["Maximum Drawdown 1year", "Max Drawdown"],
    column: "max_drawdown_1y",
    valueType: "number",
    unit: "percent",
    assetClasses: allClasses,
  },
];
