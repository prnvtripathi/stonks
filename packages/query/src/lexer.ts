import type { MetricCatalog } from "@stonks/contracts";
import type { Span, Token } from "./token";
import { diagnostic, type Diagnostic } from "./ast";

const operators = [">=", "<=", "!=", ">", "<", "=", "+", "-", "*", "/"] as const;
const word = /[A-Za-z][A-Za-z0-9_-]*/y;
const number = /(?:\d+(?:\.\d*)?|\.\d+)(%?)/y;

const folded = (value: string): string => value.trim().replace(/\s+/g, " ").toLocaleLowerCase("en-US");

export interface LexResult {
  readonly tokens: readonly Token[];
  readonly diagnostics: readonly Diagnostic[];
}

export function lex(source: string, catalog: MetricCatalog): LexResult {
  const tokens: Token[] = [];
  const diagnostics: Diagnostic[] = [];
  const aliases = catalog
    .flatMap((metric) => metric.aliases.map((alias) => ({ metric, alias, key: folded(alias) })))
    .sort((a, b) => b.alias.length - a.alias.length);
  let offset = 0;
  while (offset < source.length) {
    if (/\s/.test(source[offset] ?? "")) {
      offset += 1;
      continue;
    }
    let matched = false;
    for (const candidate of aliases) {
      const fragment = source.slice(offset, offset + candidate.alias.length);
      const boundary = source[offset + candidate.alias.length] ?? "";
      if (folded(fragment) === candidate.key && (boundary === "" || !/[A-Za-z0-9_]/.test(boundary))) {
        tokens.push({
          kind: "metric",
          lexeme: fragment,
          metricId: candidate.metric.id,
          metricLabel: candidate.metric.label,
          span: { start: offset, end: offset + candidate.alias.length },
        });
        offset += candidate.alias.length;
        matched = true;
        break;
      }
    }
    if (matched) continue;
    const start = offset;
    const char = source[offset] ?? "";
    const op = operators.find((candidate) => source.startsWith(candidate, offset));
    if (op) {
      tokens.push({ kind: "operator", lexeme: op, span: { start, end: start + op.length } });
      offset += op.length;
      continue;
    }
    if (char === "(") {
      tokens.push({ kind: "lparen", lexeme: char, span: { start, end: start + 1 } });
      offset += 1;
      continue;
    }
    if (char === ")") {
      tokens.push({ kind: "rparen", lexeme: char, span: { start, end: start + 1 } });
      offset += 1;
      continue;
    }
    number.lastIndex = offset;
    const numeric = number.exec(source);
    if (numeric) {
      const lexeme = numeric[0];
      tokens.push({
        kind: "number",
        lexeme,
        value: Number(lexeme.replace(/%$/, "")),
        span: { start, end: start + lexeme.length },
      });
      offset += lexeme.length;
      continue;
    }
    word.lastIndex = offset;
    const identifier = word.exec(source);
    if (identifier) {
      const lexeme = identifier[0];
      const kind = folded(lexeme);
      const tokenKind = kind === "and" || kind === "or" || kind === "not" ? kind : "unknown";
      tokens.push({ kind: tokenKind, lexeme, span: { start, end: start + lexeme.length } });
      offset += lexeme.length;
      continue;
    }
    tokens.push({ kind: "unknown", lexeme: char, span: { start, end: start + 1 } });
    diagnostics.push(diagnostic("UNEXPECTED_CHARACTER", `Unexpected character '${char}'.`, { start, end: start + 1 }));
    offset += 1;
  }
  tokens.push({ kind: "eof", lexeme: "", span: { start: source.length, end: source.length } });
  return { tokens, diagnostics };
}
