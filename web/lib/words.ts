// Plain words for values the API sends as codes. Check names get their words from the API
// (api/check_labels.py); this only covers an older API that sends none.

import type { CheckWords } from "./types";

const CHANNELS: Record<string, string> = {
  instagram: "Instagram",
  facebook: "Facebook",
  linkedin: "LinkedIn",
  linkedin_personal: "LinkedIn",
  linkedin_company: "LinkedIn page",
  tiktok: "TikTok",
  youtube: "YouTube",
  x: "X",
  threads: "Threads",
  pinterest: "Pinterest",
  google_business: "Google Business",
  email: "Email",
  newsletter: "Newsletter",
  blog: "Blog",
  website: "Website",
};

export function channelName(code: string): string {
  if (CHANNELS[code]) return CHANNELS[code];
  const words = code.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function channelList(codes: string[]): string {
  return [...new Set(codes.map(channelName))].join(" and ") || "No channel";
}

const DAY = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "short", timeZone: "UTC" });

export function slotDay(day: string | null | undefined): string | null {
  return day ? DAY.format(new Date(`${day}T00:00:00Z`)) : null;
}

// A finding's reason, without the character offsets and kind code the compiler prefixes.
export function detailText(reason: string): string {
  const bare = reason.replace(/^\[\d+:\d+\]\s*/, "");
  const found = /outside a slot:\s*(.+)$/.exec(bare);
  return found ? `Found ${found[1]}` : bare;
}

type Grouped<T> = { first: T; details: string[] };

// One entry per check name, with every finding's detail under it. The summary row of the copy
// compiler is dropped when the rows it summarises are on the card.
export function groupChecks<T extends { check_name: string; reason: string | null }>(rows: T[]): Grouped<T>[] {
  const hasSpecific = rows.some((r) => r.check_name.startsWith("truth.compile."));
  const out = new Map<string, Grouped<T>>();
  for (const r of rows) {
    if (hasSpecific && r.check_name === "truth.compile") continue;
    const g = out.get(r.check_name) ?? { first: r, details: [] };
    if (r.reason) g.details.push(detailText(r.reason));
    out.set(r.check_name, g);
  }
  return [...out.values()];
}

export function checkWords(check: { check_name: string } & CheckWords): { label: string; explain: string } {
  return {
    label: check.label ?? "A check",
    explain: check.explain ?? "A check asked a person to look at this.",
  };
}
