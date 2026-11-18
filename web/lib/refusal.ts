// A refused approval names what stopped it, in the card's own words, and the next step. The API's
// message carries row ids, so the names come from the card as the server sees it after the refusal.

import type { Blocking, Card } from "./types";
import { checkWords, groupChecks } from "./words";

export type Outcome = { tone: "good" | "bad"; text: string; items?: string[]; next?: string };

export const NEXT_STEP: Record<Blocking["kind"], string> = {
  fail: "Edit the text; the checks run again on the new version.",
  not_checked: "Wait for the checks to run on this version, then reopen the card.",
  problem: "Ask the operator to look at this bundle.",
};

export function refusalFor(card: Card | null, acknowledged: string[]): Outcome {
  if (card === null) {
    return { tone: "bad", text: "Not approved. The card could not be reloaded.", next: "Reload the card and try again." };
  }
  if (card.blocking.length > 0) {
    return {
      tone: "bad",
      text: "Not approved. These checks block it:",
      items: groupChecks(card.blocking).map((g) => checkWords(g.first).label),
      next: NEXT_STEP[card.blocking[0]!.kind],
    };
  }
  const unread = card.flags.filter((f) => !acknowledged.includes(f.id));
  if (unread.length > 0) {
    return {
      tone: "bad",
      text: "Not approved. These flags are not marked as read:",
      items: unread.map((f) => checkWords(f).label),
      next: "Mark each one as read, then approve.",
    };
  }
  if (!card.can_approve) {
    return { tone: "bad", text: "Not approved. This bundle is no longer waiting for review.", next: "Go back to the deck." };
  }
  return { tone: "bad", text: "Not approved. The piece changed after you opened it.", next: "Look at it again, then approve." };
}
