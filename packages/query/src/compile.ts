import type { MetricCatalog } from "@stonks/contracts";
import type { Expression } from "./ast";

export interface CompiledQuery {
  readonly whereSql: string;
  readonly params: readonly number[];
  readonly referencedMetricIds: readonly string[];
}

export class QueryCompileError extends Error {}

const safeIdentifier = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function compileQuery(ast: Expression, catalog: MetricCatalog): CompiledQuery {
  const params: number[] = [];
  const references: string[] = [];
  const metric = (id: string): string => {
    const definition = catalog.find((item) => item.id === id);
    if (!definition || !safeIdentifier.test(definition.column)) throw new QueryCompileError(`Metric '${id}' is not in the checked catalog.`);
    if (!references.includes(id)) references.push(id);
    return `\"${definition.column}\"`;
  };
  const expression = (node: Expression): string => {
    if (node.kind === "metric") return metric(node.id);
    if (node.kind === "number") {
      params.push(node.value);
      return "?";
    }
    if (node.kind === "unary") {
      return node.operator === "not" ? `(NOT ${expression(node.operand)})` : `(-${expression(node.operand)})`;
    }
    if (node.operator === "/") return `(${expression(node.left)} / NULLIF(${expression(node.right)}, 0))`;
    const sqlOperator = node.operator === "and" ? "AND" : node.operator === "or" ? "OR" : node.operator;
    return `(${expression(node.left)} ${sqlOperator} ${expression(node.right)})`;
  };
  return { whereSql: `(${expression(ast)}) IS TRUE`, params, referencedMetricIds: references };
}
