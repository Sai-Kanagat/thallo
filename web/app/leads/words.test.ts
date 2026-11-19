import { describe, expect, it } from "vitest";

import { clockText, safeAppLink } from "./words";

describe("lead desk words", () => {
  it("only follows links into the seller's own app", () => {
    expect(safeAppLink("https://wa.me/393331234567?text=ciao")).not.toBeNull();
    expect(safeAppLink("mailto:a@b.it?body=x")).not.toBeNull();
    expect(safeAppLink("javascript:alert(1)")).toBeNull();
    expect(safeAppLink("https://evil.example/")).toBeNull();
  });
  it("says how the clock stands", () => {
    expect(clockText({ running: true, due_at: null, overdue: true, minutes_left: -5 })).toBe("Reply overdue by 5 min");
    expect(clockText({ running: false, due_at: null, overdue: false, minutes_left: null, answered_in_minutes: 12 }))
      .toBe("Answered in 12 min");
  });
});
