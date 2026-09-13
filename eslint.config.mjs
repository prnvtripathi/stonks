// Root ESLint configuration for every TypeScript/TSX package.
//
// `lint` used to be an alias for `tsc --noEmit` in all four packages, which
// made it an exact duplicate of `typecheck`: nothing enforced code-quality
// rules across the TS/TSX sources, only type-correctness. This config is
// deliberately small -- the recommended TypeScript rules plus React hook
// rules -- so it catches real defects (floating promises, unsafe `any` flows,
// hook dependency mistakes) without turning into a style bikeshed.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "**/dist/**",
      "**/node_modules/**",
      "**/coverage/**",
      "**/test-results/**",
      "**/playwright-report/**",
      "apps/web/public/**",
      ".venv/**",
      "**/.venv/**",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parserOptions: { ecmaVersion: "latest", sourceType: "module" },
    },
    rules: {
      // Unused values are a real signal here; the leading-underscore escape
      // hatch keeps intentionally-ignored parameters legible.
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "no-console": ["warn", { allow: ["warn", "error"] }],
      eqeqeq: ["error", "always", { null: "ignore" }],
      "prefer-const": "error",
      "no-var": "error",
    },
  },
  {
    files: ["apps/web/src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  },
  {
    // Tests legitimately use non-null assertions and hand-built fixtures.
    files: ["**/*.test.{ts,tsx}", "**/e2e/**/*.ts"],
    rules: { "@typescript-eslint/no-explicit-any": "off", "@typescript-eslint/no-non-null-assertion": "off" },
  },
);
