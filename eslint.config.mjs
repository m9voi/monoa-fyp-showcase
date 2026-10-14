import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Phase 5B: node:test suites for the location foundation. Run by
    // `npm run test:node` (Node's built-in runner strips types); not part of
    // the app graph, tsc project, or Next build.
    "app/lib/__tests__/**",
    "app/map/__tests__/**",
  ]),
]);

export default eslintConfig;
