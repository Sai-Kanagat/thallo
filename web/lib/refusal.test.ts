import { describe, expect, it } from "vitest";

import { refusalFor } from "./refusal";
import type { Card } from "./types";

const base: Card = {
  bundle_id: "b1",
  state: "in_review",
  root_hash: "a".repeat(64),
  why: null,
  variants: [],
  flags: [],
  blocking: [],
  can_approve: true,
};

describe("refusalFor", () => {
  it("names the blocking check in plain words and gives the next step, never a row id", () => {
    const out = refusalFor(
      {
        ...base,
        can_approve: false,
        blocking: [{ kind: "fail", variant_id: "v1", check_name: "truth.compile.number_word", reason: "[0:5] 'forty'", label: "A number written as a word" }],
      },
      [],
    );
    expect(out.items).toEqual(["A number written as a word"]);
    expect(out.next).toMatch(/^Edit the text/);
    expect(JSON.stringify(out)).not.toMatch(/[0-9A-HJKMNP-TV-Z]{26}/);
  });

  it("names unread flags when nothing blocks", () => {
    const out = refusalFor(
      { ...base, flags: [{ id: "f1", variant_id: "v1", check_name: "x.y", layer: "code", reason: null, label: "A comparison" }] },
      [],
    );
    expect(out.items).toEqual(["A comparison"]);
    expect(out.next).toBe("Mark each one as read, then approve.");
  });

  it("says the piece changed when the card is otherwise clean", () => {
    expect(refusalFor(base, []).text).toMatch(/changed after you opened it/);
    expect(refusalFor(null, []).next).toBe("Reload the card and try again.");
  });
});
