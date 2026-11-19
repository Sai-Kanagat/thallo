import type { LeadClock } from "./types";

export const ROUTE: Record<string, string> = { hot: "Hot", warm: "Warm", low_fit: "Low fit" };

export const CONSENT: Record<string, string> = {
  no_region_policy: "No region policy yet: reply personally",
  policy_has_no_contact_rule_for_channel: "The region policy does not cover this channel: reply personally",
  no_contact_consent: "No consent on file for this number or address: reply personally",
  consent_withdrawn: "Consent withdrawn: do not message",
  basis_not_allowed: "Consent basis not allowed here: reply personally",
};

export const LOSS_REASONS: [string, string][] = [
  ["price", "Price"],
  ["finance_refused", "Finance refused"],
  ["chose_competitor", "Chose a competitor"],
  ["no_budget", "No budget"],
  ["timing", "Timing"],
  ["no_response", "No response"],
  ["not_a_fit", "Not a fit"],
  ["other", "Other"],
];

export function clockText(c: LeadClock): string {
  if (c.running && c.minutes_left !== null) {
    return c.overdue ? `Reply overdue by ${-c.minutes_left} min` : `Reply due in ${c.minutes_left} min`;
  }
  if (c.answered_in_minutes != null) return `Answered in ${c.answered_in_minutes} min`;
  return "No clock";
}

// Only links that open the seller's own app may be followed (the API filters too).
export function safeAppLink(link: string | null): string | null {
  if (!link) return null;
  return /^(https:\/\/wa\.me\/\d{6,15}\?text=|mailto:|sms:)/.test(link) ? link : null;
}
