import type { AssetClass, MetricCatalog, MetricDefinition } from "@stonks/contracts";
import type { BinaryNode, Diagnostic, Expression, QueryAst } from "./ast";

export interface CheckedQuery {
  readonly valid: boolean;
  readonly ast: QueryAst;
  readonly diagnostics: readonly Diagnostic[];
}

const numeric = (node: Expression, catalog: MetricCatalog): MetricDefinition | null => {
  if (node.kind === "metric") return catalog.find((metric) => metric.id === node.id) ?? null;
  return null;
};

const suggestion = (value: string, catalog: MetricCatalog): string[] => {
  const normalized = value.toLocaleLowerCase("en-US");
  return catalog
    .map((metric) => ({ label: metric.label, distance: editDistance(normalized, metric.label.toLocaleLowerCase("en-US")) }))
    .sort((a, b) => a.distance - b.distance)
    .slice(0, 2)
    .filter((item) => item.distance <= Math.max(2, normalized.length / 3))
    .map((item) => item.label);
};

function editDistance(left: string, right: string): number {
  const row = Array.from({ length: right.length + 1 }, (_, index) => index);
  for (let i = 1; i <= left.length; i += 1) {
    let previous = row[0]!;
    row[0] = i;
    for (let j = 1; j <= right.length; j += 1) {
      const current = row[j]!;
      row[j] = left[i - 1] === right[j - 1] ? previous : Math.min(previous + 1, row[j - 1]! + 1, current + 1);
      previous = current;
    }
  }
  return row[right.length]!;
}

export function typecheckQuery(ast: QueryAst, catalog: MetricCatalog, classes: readonly AssetClass[] = ["equity", "etf", "mutual_fund"]): CheckedQuery {
  const diagnostics: Diagnostic[] = [];
  const visit = (node: Expression): "number" | "boolean" | "invalid" => {
    if (node.kind === "number") {
      if (!Number.isFinite(node.value)) diagnostics.push({ code: "INVALID_NUMBER", message: "Number must be finite.", span: node.span });
      return "number";
    }
    if (node.kind === "metric") {
      const metric = numeric(node, catalog);
      if (!metric) {
        diagnostics.push({ code: "UNKNOWN_METRIC", message: `Unknown metric '${node.label}'.`, span: node.span, suggestions: suggestion(node.label, catalog) });
        return "invalid";
      }
      if (!classes.some((assetClass) => metric.assetClasses.includes(assetClass))) {
        diagnostics.push({ code: "METRIC_NOT_APPLICABLE", severity: "warning", message: `${metric.label} is not applicable to the selected asset classes.`, span: node.span });
      }
      return "number";
    }
    if (node.kind === "unary") {
      const operand = visit(node.operand);
      if (node.operator === "not") {
        if (operand !== "boolean") diagnostics.push({ code: "TYPE_MISMATCH", message: "NOT requires a comparison or Boolean expression.", span: node.span });
        return operand === "invalid" ? "invalid" : "boolean";
      }
      if (operand !== "number") diagnostics.push({ code: "TYPE_MISMATCH", message: "Unary minus requires a numeric expression.", span: node.span });
      return operand === "invalid" ? "invalid" : "number";
    }
    const left = visit(node.left);
    const right = visit(node.right);
    const booleanOperator = node.operator === "and" || node.operator === "or";
    const comparison = [">", ">=", "<", "<=", "=", "!="].includes(node.operator);
    if (booleanOperator) {
      if (left !== "boolean" || right !== "boolean") diagnostics.push({ code: "TYPE_MISMATCH", message: `${node.operator.toUpperCase()} requires Boolean expressions.`, span: node.span });
      return left === "invalid" || right === "invalid" ? "invalid" : "boolean";
    }
    if (comparison) {
      if (left !== "number" || right !== "number") diagnostics.push({ code: "TYPE_MISMATCH", message: "Comparisons require numeric expressions.", span: node.span });
      return left === "invalid" || right === "invalid" ? "invalid" : "boolean";
    }
    if (left !== "number" || right !== "number") diagnostics.push({ code: "TYPE_MISMATCH", message: "Arithmetic requires numeric expressions.", span: node.span });
    return left === "invalid" || right === "invalid" ? "invalid" : "number";
  };
  const type = visit(ast);
  if (type !== "boolean") diagnostics.push({ code: "ROOT_NOT_BOOLEAN", message: "A screen must evaluate to a Boolean predicate.", span: ast.span });
  return { valid: !diagnostics.some((item) => item.severity !== "warning"), ast, diagnostics };
}

export const isBinary = (node: Expression): node is BinaryNode => node.kind === "binary";
