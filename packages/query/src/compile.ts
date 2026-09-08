import type { MetricCatalog } from "@stonks/contracts";
import type { Expression } from "./ast";

export interface CompiledQuery {
  readonly whereSql: string;
  readonly params: readonly (number | string | null)[];
  readonly referencedMetricIds: readonly string[];
}

export interface CompileOptions {
  /** `wide` is a checked projection; `eav` targets local latest_metrics; `snapshot` targets the compact remote JSON projection. */
  readonly relation?: "wide" | "eav" | "snapshot";
  /** Required by D1/EAV execution; omitted values bind NULL and match no rows. */
  readonly datasetId?: string;
  /** EAV callers may apply the dataset predicate in their base relation. */
  readonly includeDatasetFilter?: boolean;
}

export class QueryCompileError extends Error {}

const safeIdentifier = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function compileQuery(ast: Expression, catalog: MetricCatalog, options: CompileOptions = {}): CompiledQuery {
  const params: (number | string | null)[] = [];
  const references: string[] = [];
  const eav = options.relation === "eav";
  const snapshot = options.relation === "snapshot";
  if (eav && options.includeDatasetFilter !== false) params.push(options.datasetId ?? null);
  const metric = (id: string): string => {
    const definition = catalog.find((item) => item.id === id);
    if (!definition || !safeIdentifier.test(definition.column)) throw new QueryCompileError(`Metric '${id}' is not in the checked catalog.`);
    if (!references.includes(id)) references.push(id);
    if (eav) {
      params.push(id);
      return `(SELECT CASE WHEN m.state = 'present' THEN m.value END FROM latest_metrics AS m WHERE m.dataset_id = i.dataset_id AND m.instrument_id = i.instrument_id AND m.metric = ?)`;
    }
    if (snapshot) return `json_extract(s.metric_values_json, '$.${id}')`;
    return `"${definition.column}"`;
  };
  const expression = (node: Expression): string => {
    if (node.kind === "metric") return metric(node.id);
    if (node.kind === "number") {
      params.push(node.percent ? node.value / 100 : node.value);
      return "?";
    }
    if (node.kind === "unary") {
      return node.operator === "not" ? `(NOT ${expression(node.operand)})` : `(-${expression(node.operand)})`;
    }
    if (node.operator === "/") return `(${expression(node.left)} / NULLIF(${expression(node.right)}, 0))`;
    const sqlOperator = node.operator === "and" ? "AND" : node.operator === "or" ? "OR" : node.operator;
    return `(${expression(node.left)} ${sqlOperator} ${expression(node.right)})`;
  };
  const predicate = `(${expression(ast)}) IS TRUE`;
  return { whereSql: eav && options.includeDatasetFilter !== false ? `(i.dataset_id = ? AND ${predicate})` : predicate, params, referencedMetricIds: references };
}

/**
 * Compiles a predicate for `SELECT ... FROM instruments AS i` over the EAV
 * publication schema. It is intentionally a separate named helper so callers
 * cannot accidentally run an EAV predicate without the required instrument
 * correlation context.
 */
export function compileEavQuery(ast: Expression, catalog: MetricCatalog, datasetId: string): CompiledQuery {
  return compileQuery(ast, catalog, { relation: "eav", datasetId });
}
