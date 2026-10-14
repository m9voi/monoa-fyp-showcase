// Phase 5H-B4 (M1) — safeInternalPath: only local paths survive.
import { test } from "node:test";
import assert from "node:assert/strict";

import { safeInternalPath } from "../safeRedirect.ts";

test("accepts real local application paths unchanged", () => {
  for (const p of ["/", "/schedule", "/finance", "/map?focus=abc", "/activity#section",
                   "/finance/history?range=week"]) {
    assert.equal(safeInternalPath(p), p);
  }
});

test("trims surrounding whitespace", () => {
  assert.equal(safeInternalPath("  /schedule  "), "/schedule");
});

test("null / undefined / non-string / empty -> /", () => {
  assert.equal(safeInternalPath(null), "/");
  assert.equal(safeInternalPath(undefined), "/");
  assert.equal(safeInternalPath(""), "/");
  // @ts-expect-error deliberate wrong type
  assert.equal(safeInternalPath(42), "/");
});

test("external / network-path destinations never pass", () => {
  const attacks = [
    "https://evil.example",
    "http://evil.example",
    "//evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "https:evil.example",
    "javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "  //evil.example",
    "/\tevil.example",
    "/\r/evil",
    "/\n//evil",
  ];
  for (const a of attacks) {
    const out = safeInternalPath(a);
    assert.equal(out, "/", `expected "/" for ${JSON.stringify(a)}, got ${JSON.stringify(out)}`);
  }
});

test("over-long input -> /", () => {
  assert.equal(safeInternalPath("/" + "a".repeat(5000)), "/");
});

test("values as URLSearchParams.get() would already have decoded them", () => {
  // ?next=%2F%2Fevil.com     -> "//evil.com"
  // ?next=%2F%5Cevil.com     -> "/\\evil.com"
  // ?next=%09//evil          -> "\t//evil"
  // ?next=%00/x              -> "\x00/x"
  // ?next=/%2E%2E/admin      -> "/../admin"  (same-origin, harmless path)
  const decoded: Array<[string, string]> = [
    [decodeURIComponent("%2F%2Fevil.com"), "/"],
    [decodeURIComponent("%2F%5Cevil.com"), "/"],
    [decodeURIComponent("%09%2F%2Fevil"), "/"],
    [decodeURIComponent("%00%2Fx"), "/"],
    [decodeURIComponent("%0D%0A/evil"), "/"],
  ];
  for (const [input, want] of decoded) {
    assert.equal(safeInternalPath(input), want, JSON.stringify(input));
  }
  // ".." segments stay on-origin; the browser/router resolves them locally.
  const dots = safeInternalPath("/../admin");
  assert.ok(dots.startsWith("/") && dots[1] !== "/");
});

test("result always starts with exactly one slash", () => {
  for (const a of ["/x", "//x", "/\\x", "https://x", "/ok?a=//b"]) {
    const out = safeInternalPath(a);
    assert.ok(out.startsWith("/") && out[1] !== "/" && out[1] !== "\\");
  }
});
