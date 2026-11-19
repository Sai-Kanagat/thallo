// Shapes the growth API sends (api/growth.py). Kept beside the page so WP-15 owns them.

export type Stage = {
  name: string;
  label: string;
  rate: string;
  low: string;
  high: string;
  tag: "guessed" | "measured";
  outcomes: number;
};

export type Summary = {
  currency: string;
  allowable_cost: string;
  allowable_low: string;
  allowable_high: string;
  tag: "guessed" | "measured";
  formula: string;
  channel_targets: { role: string; label: string; multiple: string; max_cost: string; tag: string }[];
  stop_after_spend: string;
  stop_rule: string;
};

export type GrowthModelView = {
  version: number;
  play_code: string | null;
  stages: Stage[];
  bottleneck: string | null;
  margin_per_won: string;
  share_of_margin: string;
  summary: Summary;
};

export type PolicyView = {
  status: "ok" | "contradicts" | "no_policy";
  region_code: string;
  version: number | null;
  problems: string[];
  not_checked: string[];
};

export type PlayView = {
  code: string;
  name: string;
  status: string;
  fits: string;
  channels: string[];
  formats: string[];
  booked_definition: string;
  bottlenecks_in_order: string[];
  pillar_mix: Record<string, string>;
  headline_metric: string;
  metrics: string[];
  never: string[];
  fit_range: [number, number];
  evidence: string;
  evidence_label: string;
  saved: boolean;
  policy: PolicyView;
  invalid?: string[];
};

export type GrowthView = {
  currency: string;
  region_code: string;
  model: GrowthModelView | null;
  plays: PlayView[];
  stages: { name: string; label: string; required: boolean }[];
  default_share: string;
  measured_after: number;
  guess_label: string;
  open_questions: string[];
  can_edit: boolean;
};
