import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Card } from "@/lib/types";

import { ReviewCard } from "./ReviewCard";

const HASH = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90";

function card(over: Partial<Card> = {}): Card {
  return {
    bundle_id: "b1",
    state: "in_review",
    root_hash: HASH,
    why: "Range is the top question",
    can_approve: true,
    blocking: [],
    flags: [
      { id: "f1", variant_id: "v1", check_name: "truth.compile.flag_word", layer: "code", reason: "\"il top\" is a superlative" },
      { id: "f2", variant_id: "v1", check_name: "grader.voice", layer: "grader", reason: null },
    ],
    variants: [
      {
        id: "v1",
        channel: "instagram",
        language: "it",
        state: "review",
        version: 1,
        variant_hash: "f".repeat(64),
        text: "Fino a 120 km <script>alert(1)</script> con una carica",
        source_text: "Fino a {{C-0001}} con una carica",
        media: [],
        links: [],
        claims: [{ code: "C-0001", value: "120", unit: "km", state: "verified", source_id: null }],
      },
    ],
    ...over,
  };
}

const noop = () => {};

describe("ReviewCard", () => {
  it("shows the root hash it will approve", () => {
    render(<ReviewCard card={card()} onApprove={noop} onEdit={noop} onReject={noop} />);
    const hash = screen.getByTestId("root-hash");
    expect(hash).toHaveTextContent("a1b2c3d4 e5f60718");
    expect(hash).toHaveAttribute("title", HASH);
  });

  it("keeps approve disabled until every flag is acknowledged, then sends their ids", () => {
    const onApprove = vi.fn();
    let t = 0;
    render(<ReviewCard card={card()} onApprove={onApprove} onEdit={noop} onReject={noop} now={() => t} />);
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();
    expect(screen.getByText("Mark 2 flags as read to approve.")).toBeInTheDocument();

    const marks = screen.getAllByRole("button", { name: "Mark as read" });
    fireEvent.click(marks[0]!);
    expect(approve).toBeDisabled();
    fireEvent.click(screen.getAllByRole("button", { name: "Mark as read" })[0]!);
    expect(approve).toBeEnabled();

    // un-reading one disables it again
    fireEvent.click(screen.getAllByRole("button", { name: "Read" })[0]!);
    expect(approve).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Mark as read" }));

    t = 42_000;
    fireEvent.click(approve);
    expect(onApprove).toHaveBeenCalledTimes(1);
    const [ids, seconds] = onApprove.mock.calls[0]!;
    expect([...ids].sort()).toEqual(["f1", "f2"]);
    expect(seconds).toBe(42);
  });

  it("blocks on a FAIL or not checked and names the check and the next step", () => {
    render(
      <ReviewCard
        card={card({
          flags: [],
          can_approve: false,
          blocking: [
            { kind: "fail", variant_id: "v1", check_name: "truth.compile.unverified_claim", reason: "C-0002 is not verified" },
            { kind: "not_checked", variant_id: "v1", check_name: "coverage.media", reason: "no media check has run" },
          ],
        })}
        onApprove={noop}
        onEdit={noop}
        onReject={noop}
      />,
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByText("truth.compile.unverified_claim")).toBeInTheDocument();
    expect(screen.getByText("coverage.media")).toBeInTheDocument();
    expect(screen.getByText(/Edit the text; the checks run again/)).toBeInTheDocument();
  });

  it("renders untrusted text as text, never as HTML", () => {
    const { container } = render(<ReviewCard card={card()} onApprove={noop} onEdit={noop} onReject={noop} />);
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByTestId("piece-text")).toHaveTextContent("<script>alert(1)</script>");
  });

  it("edits the source text with slots and rejects with one tap on a reason", () => {
    const onEdit = vi.fn();
    const onReject = vi.fn();
    render(<ReviewCard card={card()} onApprove={noop} onEdit={onEdit} onReject={onReject} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit this piece" }));
    const box = screen.getByRole("textbox");
    expect(box).toHaveValue("Fino a {{C-0001}} con una carica");
    fireEvent.change(box, { target: { value: "Fino a {{C-0001}}." } });
    fireEvent.click(screen.getByRole("button", { name: "Save and recheck" }));
    expect(onEdit).toHaveBeenCalledWith("v1", "Fino a {{C-0001}}.");

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.click(screen.getByRole("button", { name: "Not how we talk" }));
    expect(onReject).toHaveBeenCalledWith("off_voice");
  });

  it("approves a card with no flags straight away", () => {
    const onApprove = vi.fn();
    render(<ReviewCard card={card({ flags: [] })} onApprove={onApprove} onEdit={noop} onReject={noop} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onApprove).toHaveBeenCalledWith([], expect.any(Number));
  });
});
