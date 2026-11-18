// Shapes the API returns. Every string in them is untrusted (copy, lead text, brand text):
// React escapes it on render and nothing here is ever rendered as HTML.

export type Membership = { tenant_id: string; tenant_name: string; role: string; display_name: string };

export type SessionView = {
  authenticated: boolean;
  csrf_token: string;
  tenant: Membership | null;
  memberships?: Membership[];
};

// label and explain are the plain words for check_name, from the one table in api/check_labels.py.
export type CheckWords = { label?: string; explain?: string };

export type Flag = { id: string; variant_id: string; check_name: string; layer: string; reason: string | null } & CheckWords;

export type Blocking = {
  kind: "fail" | "not_checked" | "problem";
  variant_id: string | null;
  check_name: string;
  reason: string | null;
} & CheckWords;

export type CardSlot = { starts_at: string; day: string; pillar: string | null; stage: string };

export type CardMedia = {
  id: string | null;
  position: number;
  sha256: string;
  kind: string;
  mime: string;
  alt_text: string | null;
  disclosure_label: string | null;
  url: string | null;
};

export type CardClaim = { code: string; value: string; unit: string | null; state: string; source_id: string | null };

export type CardVariant = {
  id: string;
  channel: string;
  language: string;
  state: string;
  version: number;
  variant_hash: string;
  text: string;
  source_text: string;
  media: CardMedia[];
  links: { url: string; destination: string | null }[];
  claims: CardClaim[];
};

export type Card = {
  bundle_id: string;
  state: string;
  root_hash: string | null;
  why: string | null;
  reason?: string | null;
  headline?: string | null;
  slot?: CardSlot | null;
  variants: CardVariant[];
  flags: Flag[];
  blocking: Blocking[];
  can_approve: boolean;
};

export type BundleSummary = {
  bundle_id: string;
  created_at: string;
  state: string;
  channels: string[];
  variant_count: number;
  headline?: string | null;
  thumbnail_url?: string | null;
  slot?: CardSlot | null;
  status?: "ready" | "flag" | "blocked";
  flag_count?: number;
  blocking_count?: number;
};

export type Slot = {
  id: string;
  channel: string;
  stage: string;
  state: string;
  starts_at: string;
  day: string;
  pillar: string | null;
  variant_id: string | null;
  reserved_for_experiment: boolean;
  can_kill: boolean;
  can_move: boolean;
};

export type WeekPlan = {
  week_start: string;
  days: string[];
  slots: Slot[];
  stage_mix: { stage: string; slots: number; share: number; target: number | null }[];
  can_edit: boolean;
};

export type ConnectionRow = {
  id: string;
  capability: string;
  provider: string;
  mode: string;
  model: string | null;
  role: string | null;
  scope: string;
  priority: number;
  enabled: boolean;
  settings: Record<string, string | number | boolean>;
  has_secret: boolean;
  health: "connected" | "down" | null;
  cost_this_month_eur: string;
  quota: string | null;
  fixed_origin: string | null;
  is_floor: boolean;
};

export type ProviderOption = {
  capability: string;
  provider: string;
  mode: string;
  settings: string[];
  takes_secret: boolean;
  fixed_origin: string | null;
};

export const REJECTION_REASONS: { value: string; label: string }[] = [
  { value: "off_voice", label: "Not how we talk" },
  { value: "generic", label: "Too generic" },
  { value: "wrong_fact", label: "A fact is wrong" },
  { value: "weak_visual", label: "Weak picture" },
  { value: "wrong_timing", label: "Wrong moment" },
  { value: "not_interesting", label: "Not interesting" },
  { value: "other", label: "Something else" },
];
