import type { AssetClass, MetricCatalog, MetricDefinition } from "@stonks/contracts";
import type { BinaryNode, Diagnostic, Expression, QueryAst } from "./ast";

export interface CheckedQuery {
  readonly valid: boolean;
  readonly ast: QueryAst;
  readonly diagnostics: readonly Diagnostic[];
}

type ValueUnit = "scalar" | "percent" | "count" | "currency" | "ratio";
type TypeInfo = { kind: "number"; unit: ValueUnit } | { kind: "boolean" } | { kind: "invalid" };

const isNumber = (value: TypeInfo): value is Extract<TypeInfo, { kind: "number" }> => value.kind === "number";
const isCompatible = (left: ValueUnit, right: ValueUnit): boolean => left === right || left === "scalar" || right === "scalar";
const combineProduct = (left: ValueUnit, right: ValueUnit): ValueUnit | null => {
  if (left === "scalar") return right;
  if (right === "scalar") return left;
  if (left === right && left === "percent") return "ratio";
  return null;
};
const combineQuotient = (left: ValueUnit, right: ValueUnit): ValueUnit | null => {
  if (right === "scalar") return left;
  if (left === right) return "scalar";
  return null;
};

/**
 * A bare numeric literal (optionally negated) written where a percent value
 * belongs. Percent metrics are stored as fractions, so `Return over 1day > 3`
 * silently means "> 300%" -- a 100x screening error. Returning the literal
 * here lets the checker suggest the `%` the author almost certainly meant.
 */
const bareNumberLiteral = (node: Expression): number | null => {
  if (node.kind === "number") return node.percent ? null : node.value;
  if (node.kind === "unary" && node.operator === "-") {
    const value = bareNumberLiteral(node.operand);
    return value === null ? null : -value;
  }
  return null;
};

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
  const visit = (node: Expression): TypeInfo => {
    if (node.kind === "number") {
      if (!Number.isFinite(node.value)) diagnostics.push({ code: "INVALID_NUMBER", message: "Number must be finite.", span: node.span });
      return { kind: "number", unit: node.percent ? "percent" : "scalar" };
    }
    if (node.kind === "metric") {
      const metric = numeric(node, catalog);
      if (!metric) {
        diagnostics.push({ code: "UNKNOWN_METRIC", message: `Unknown metric '${node.label}'.`, span: node.span, suggestions: suggestion(node.label, catalog) });
        return { kind: "invalid" };
      }
      if (!classes.some((assetClass) => metric.assetClasses.includes(assetClass))) {
        diagnostics.push({ code: "METRIC_NOT_APPLICABLE", severity: "warning", message: `${metric.label} is not applicable to the selected asset classes.`, span: node.span });
      }
      return { kind: "number", unit: metric.unit === "number" ? "scalar" : metric.unit };
    }
    if (node.kind === "unary") {
      const operand = visit(node.operand);
      if (node.operator === "not") {
        if (operand.kind !== "boolean") diagnostics.push({ code: "TYPE_MISMATCH", message: "NOT requires a comparison or Boolean expression.", span: node.span });
        return operand.kind === "invalid" ? { kind: "invalid" } : { kind: "boolean" };
      }
      if (!isNumber(operand)) diagnostics.push({ code: "TYPE_MISMATCH", message: "Unary minus requires a numeric expression.", span: node.span });
      return isNumber(operand) ? operand : { kind: "invalid" };
    }
    const left = visit(node.left);
    const right = visit(node.right);
    const booleanOperator = node.operator === "and" || node.operator === "or";
    const comparison = [">", ">=", "<", "<=", "=", "!="].includes(node.operator);
    if (booleanOperator) {
      if (left.kind !== "boolean" || right.kind !== "boolean") diagnostics.push({ code: "TYPE_MISMATCH", message: `${node.operator.toUpperCase()} requires Boolean expressions.`, span: node.span });
      return left.kind === "invalid" || right.kind === "invalid" ? { kind: "invalid" } : { kind: "boolean" };
    }
    if (comparison) {
      if (!isNumber(left) || !isNumber(right)) diagnostics.push({ code: "TYPE_MISMATCH", message: "Comparisons require numeric expressions.", span: node.span });
      else if (!isCompatible(left.unit, right.unit)) diagnostics.push({ code: "UNIT_MISMATCH", message: `Cannot compare ${left.unit} with ${right.unit}; use a compatible unit.`, span: node.span });
      else {
        const percentSide = left.unit === "percent" ? right : right.unit === "percent" ? left : null;
        const literalNode = left.unit === "percent" ? node.right : node.left;
        const literal = percentSide?.kind === "number" && percentSide.unit === "scalar" ? bareNumberLiteral(literalNode) : null;
        if (literal !== null) diagnostics.push({ code: "UNIT_MISMATCH", message: `Percent values are fractions, so ${literal} means ${literal * 100}%. Add a % suffix to compare against a percentage.`, span: literalNode.span, suggestions: [`${literal}%`] });
      }
      return left.kind === "invalid" || right.kind === "invalid" ? { kind: "invalid" } : { kind: "boolean" };
    }
    if (!isNumber(left) || !isNumber(right)) {
      diagnostics.push({ code: "TYPE_MISMATCH", message: "Arithmetic requires numeric expressions.", span: node.span });
      return { kind: "invalid" };
    }
    const unit = node.operator === "+" || node.operator === "-"
      ? isCompatible(left.unit, right.unit) && left.unit !== "scalar" ? left.unit : isCompatible(left.unit, right.unit) ? right.unit : null
      : node.operator === "*" ? combineProduct(left.unit, right.unit)
      : combineQuotient(left.unit, right.unit);
    if (!unit) diagnostics.push({ code: "UNIT_MISMATCH", message: `Incompatible units ${left.unit} and ${right.unit}.`, span: node.span });
    return unit ? { kind: "number", unit } : { kind: "invalid" };
  };
  const type = visit(ast);
  if (type.kind !== "boolean") diagnostics.push({ code: "ROOT_NOT_BOOLEAN", message: "A screen must evaluate to a Boolean predicate.", span: ast.span });
  return { valid: !diagnostics.some((item) => item.severity !== "warning"), ast, diagnostics };
}

export const isBinary = (node: Expression): node is BinaryNode => node.kind === "binary";
