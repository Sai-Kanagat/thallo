import { describe, expect, it } from "vitest";

import { buildCsp } from "./proxy";

describe("content security policy", () => {
  it("allows script and style only by nonce or same origin, and nothing inline", () => {
    const csp = buildCsp("abc123", false);
    expect(csp).toContain("script-src 'self' 'nonce-abc123' 'strict-dynamic'");
    expect(csp).toContain("style-src 'self' 'nonce-abc123'");
    expect(csp).toContain("style-src-attr 'none'");
    expect(csp).toContain("connect-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain("unsafe-inline");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).not.toMatch(/https?:\/\//); // no third-party origin anywhere
  });
});
