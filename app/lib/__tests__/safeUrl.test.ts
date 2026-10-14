// Phase 5H-B4 (L1) — safeExternalHref: only absolute http/https URLs.
import { test } from "node:test";
import assert from "node:assert/strict";

import { safeExternalHref } from "../safeUrl.ts";

test("passes absolute http/https URLs", () => {
  assert.equal(safeExternalHref("https://example.com"), "https://example.com/");
  assert.equal(safeExternalHref("http://example.com/x?q=1"), "http://example.com/x?q=1");
  assert.equal(safeExternalHref("  https://example.com/y  "), "https://example.com/y");
});

test("rejects dangerous or non-web schemes", () => {
  for (const bad of [
    "javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "file:///etc/passwd",
    "vbscript:msgbox(1)",
    "about:blank",
  ]) {
    assert.equal(safeExternalHref(bad), null, bad);
  }
});

test("rejects relative / malformed / empty / non-string", () => {
  assert.equal(safeExternalHref("/local/path"), null);
  assert.equal(safeExternalHref("example.com"), null);
  assert.equal(safeExternalHref("://nohost"), null);
  assert.equal(safeExternalHref(""), null);
  assert.equal(safeExternalHref("   "), null);
  assert.equal(safeExternalHref(null), null);
  assert.equal(safeExternalHref(undefined), null);
});

test("unusual but http/https URLs never yield a dangerous scheme", () => {
  // userinfo is preserved but the SCHEME is still http/https (not executable)
  const u = safeExternalHref("http://user:pass@example.com/x");
  assert.ok(u !== null && (u.startsWith("http://") || u.startsWith("https://")));
  // scheme-smuggling attempts
  assert.equal(safeExternalHref("javascript:alert(1)//https://ok.com"), null);
  assert.equal(safeExternalHref("java\tscript:alert(1)"), null);
  assert.equal(safeExternalHref("javascript:alert(1)"), null);
});
