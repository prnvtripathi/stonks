import type { Expression } from "./ast";

export type TriState = "true" | "false" | "unknown";
export type QueryRow = Readonly<Record<string, number | null | undefined>>;
type Value = number | null;

const triNot = (value: TriState): TriState => (value === "unknown" ? "unknown" : value === "true" ? "false" : "true");
const triAnd = (left: TriState, right: TriState): TriState => left === "false" || right === "false" ? "false" : left === "unknown" || right === "unknown" ? "unknown" : "true";
const triOr = (left: TriState, right: TriState): TriState => left === "true" || right === "true" ? "true" : left === "unknown" || right === "unknown" ? "unknown" : "false";

export function evaluateQuery(ast: Expression, row: QueryRow): TriState {
  const value = (node: Expression): Value => {
    if (node.kind === "number") return node.value;
    if (node.kind === "metric") {
      const result = row[node.id];
      return typeof result === "number" && Number.isFinite(result) ? result : null;
    }
    if (node.kind === "unary") {
      if (node.operator === "not") return null;
      const operand = value(node.operand);
      return operand === null ? null : -operand;
    }
    const left = value(node.left);
    const right = value(node.right);
    if (left === null || right === null) return null;
    if (node.operator === "+") return left + right;
    if (node.operator === "-") return left - right;
    if (node.operator === "*") return left * right;
    if (node.operator === "/") return right === 0 ? null : left / right;
    return null;
  };
  const bool = (node: Expression): TriState => {
    if (node.kind === "unary" && node.operator === "not") return triNot(bool(node.operand));
    if (node.kind === "binary" && node.operator === "and") return triAnd(bool(node.left), bool(node.right));
    if (node.kind === "binary" && node.operator === "or") return triOr(bool(node.left), bool(node.right));
    if (node.kind !== "binary") return "unknown";
    const left = value(node.left);
    const right = value(node.right);
    if (left === null || right === null) return "unknown";
    switch (node.operator) {
      case ">": return left > right ? "true" : "false";
      case ">=": return left >= right ? "true" : "false";
      case "<": return left < right ? "true" : "false";
      case "<=": return left <= right ? "true" : "false";
      case "=": return left === right ? "true" : "false";
      case "!=": return left !== right ? "true" : "false";
      default: return "unknown";
    }
  };
  return bool(ast);
}
