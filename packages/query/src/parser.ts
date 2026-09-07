import { diagnostic, type Diagnostic, type QueryAst, type Expression } from "./ast";
import { lex } from "./lexer";
import type { MetricCatalog } from "@stonks/contracts";
import { DEFAULT_METRIC_CATALOG } from "@stonks/contracts";
import type { Token } from "./token";

export interface ParseResult {
  readonly value: QueryAst | null;
  readonly diagnostics: readonly Diagnostic[];
}

const precedence: Readonly<Record<string, number>> = {
  or: 1,
  and: 2,
  ">": 3,
  ">=": 3,
  "<": 3,
  "<=": 3,
  "=": 3,
  "!=": 3,
  "+": 4,
  "-": 4,
  "*": 5,
  "/": 5,
};

export function parseQuery(source: string, catalog: MetricCatalog): ParseResult;
export function parseQuery(source: string): ParseResult;
export function parseQuery(source: string, catalog?: MetricCatalog): ParseResult {
  const activeCatalog = catalog ?? DEFAULT_METRIC_CATALOG;
  const lexed = lex(source, activeCatalog);
  const tokens = lexed.tokens;
  const diagnostics = [...lexed.diagnostics];
  let position = 0;
  const current = (): Token => tokens[position] ?? tokens[tokens.length - 1]!;
  const consume = (): Token => {
    const token = current();
    position += 1;
    return token;
  };
  const parsePrimary = (): Expression | null => {
    const token = current();
    if (token.kind === "number") {
      consume();
      return { kind: "number", value: token.value ?? NaN, percent: token.lexeme.endsWith("%"), span: token.span };
    }
    if (token.kind === "metric") {
      consume();
      return { kind: "metric", id: token.metricId!, label: token.metricLabel!, span: token.span };
    }
    if (token.kind === "unknown") {
      consume();
      diagnostics.push(diagnostic("UNKNOWN_METRIC", `Unknown metric '${token.lexeme}'.`, token.span, {
        suggestions: activeCatalog
          .map((metric) => ({ label: metric.label, distance: distance(token.lexeme, metric.label) }))
          .sort((left, right) => left.distance - right.distance)
          .slice(0, 2)
          .map((item) => item.label),
      }));
      return { kind: "metric", id: token.lexeme, label: token.lexeme, span: token.span };
    }
    if (token.kind === "lparen") {
      const start = consume().span.start;
      const expression = parseExpression(0);
      if (current().kind !== "rparen") {
        diagnostics.push(diagnostic("EXPECTED_RPAREN", "Expected closing parenthesis.", current().span, { expected: [")"] }));
        return expression;
      }
      const end = consume().span.end;
      return expression ? { ...expression, span: { start, end } } : null;
    }
    diagnostics.push(diagnostic("EXPECTED_EXPRESSION", "Expected a metric, number, or parenthesized expression.", token.span, { expected: ["metric", "number", "("] }));
    return null;
  };
  const parsePrefix = (): Expression | null => {
    const token = current();
    if (token.kind === "not" || (token.kind === "operator" && token.lexeme === "-")) {
      consume();
      const operand = parsePrefix();
      return operand ? { kind: "unary", operator: token.kind === "not" ? "not" : "-", operand, span: { start: token.span.start, end: operand.span.end } } : null;
    }
    return parsePrimary();
  };
  function parseExpression(minPrecedence: number): Expression | null {
    let left = parsePrefix();
    if (!left) return null;
    while (true) {
      const token = current();
      const operator = token.kind === "and" || token.kind === "or" ? token.kind : token.kind === "operator" ? token.lexeme : "";
      const level = precedence[operator];
      if (level === undefined || level < minPrecedence) break;
      consume();
      const right = parseExpression(level + 1);
      if (!right) return null;
      left = { kind: "binary", operator: operator as never, left, right, span: { start: left.span.start, end: right.span.end } };
    }
    return left;
  }
  const value = parseExpression(0);
  if (current().kind !== "eof") {
    const token = current();
    diagnostics.push(diagnostic("UNEXPECTED_TOKEN", `Unexpected token '${token.lexeme}'.`, token.span));
  }
  return { value: diagnostics.some((item) => item.severity !== "warning") ? null : value, diagnostics };
}

function distance(left: string, right: string): number {
  const source = left.toLocaleLowerCase("en-US");
  const target = right.toLocaleLowerCase("en-US");
  const row = Array.from({ length: target.length + 1 }, (_, index) => index);
  for (let i = 1; i <= source.length; i += 1) {
    let previous = row[0]!;
    row[0] = i;
    for (let j = 1; j <= target.length; j += 1) {
      const current = row[j]!;
      row[j] = source[i - 1] === target[j - 1] ? previous : Math.min(previous + 1, row[j - 1]! + 1, current + 1);
      previous = current;
    }
  }
  return row[target.length]!;
}
