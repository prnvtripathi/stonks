import type { Span } from "./token";

export type BinaryOperator = "and" | "or" | ">" | ">=" | "<" | "<=" | "=" | "!=" | "+" | "-" | "*" | "/";
export type UnaryOperator = "not" | "-";

export interface NumberNode {
  readonly kind: "number";
  readonly value: number;
  readonly percent: boolean;
  readonly span: Span;
}

export interface MetricNode {
  readonly kind: "metric";
  readonly id: string;
  readonly label: string;
  readonly span: Span;
}

export interface UnaryNode {
  readonly kind: "unary";
  readonly operator: UnaryOperator;
  readonly operand: Expression;
  readonly span: Span;
}

export interface BinaryNode {
  readonly kind: "binary";
  readonly operator: BinaryOperator;
  readonly left: Expression;
  readonly right: Expression;
  readonly span: Span;
}

export type Expression = NumberNode | MetricNode | UnaryNode | BinaryNode;
export type QueryAst = Expression;

export interface Diagnostic {
  readonly code: string;
  readonly message: string;
  readonly span: Span;
  readonly expected?: readonly string[];
  readonly suggestions?: readonly string[];
  readonly severity?: "error" | "warning";
}

export const diagnostic = (
  code: string,
  message: string,
  span: Span,
  extra: Omit<Diagnostic, "code" | "message" | "span"> = {},
): Diagnostic => ({ code, message, span, ...extra });

export function printAst(node: Expression): string {
  if (node.kind === "metric") return node.label;
  if (node.kind === "number") return `${node.value}${node.percent ? "%" : ""}`;
  if (node.kind === "unary") return `(${node.operator === "not" ? "NOT " : "-"}${printAst(node.operand)})`;
  return `(${printAst(node.left)} ${node.operator === "and" ? "AND" : node.operator === "or" ? "OR" : node.operator} ${printAst(node.right)})`;
}
