export type TokenKind =
  | "metric"
  | "number"
  | "and"
  | "or"
  | "not"
  | "operator"
  | "lparen"
  | "rparen"
  | "eof"
  | "unknown";

export interface Span {
  readonly start: number;
  readonly end: number;
}

export interface Token {
  readonly kind: TokenKind;
  readonly lexeme: string;
  readonly span: Span;
  readonly value?: number;
  readonly metricId?: string;
  readonly metricLabel?: string;
}
