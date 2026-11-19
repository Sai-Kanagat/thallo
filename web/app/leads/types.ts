// Shapes the lead desk API returns (api/leads.py). Local to this screen.

export type LeadClock = {
  running: boolean;
  due_at: string | null;
  overdue: boolean;
  minutes_left: number | null;
  answered_in_minutes?: number | null;
};

export type LeadRow = {
  id: string;
  name: string;
  state: string;
  route: "hot" | "warm" | "low_fit" | null;
  fit: number;
  heat: number;
  clock: LeadClock;
  channel: "whatsapp" | "email" | "sms" | null;
  may_message: boolean;
  consent_reason: string | null;
  drafts: number;
  created_at: string;
};

export type LeadList = { set_up: boolean; leads: LeadRow[] };

export type LeadQuestion = {
  key: string;
  ask: string;
  type: "choice" | "choice_from_catalogue" | "text";
  options: string[];
  answer: string | null;
};

export type LeadMessage = {
  id: string;
  kind: "acknowledgement" | "reply_draft" | "qualification" | "nurture" | "reminder";
  channel: string;
  state: string;
  body: string;
  body_sha256: string;
  send_state: "approved" | "opened" | "sent" | "failed" | null;
  editable: boolean;
};

export type LeadDetail = LeadRow & {
  person: { phone: string | null; email: string | null };
  questions: LeadQuestion[];
  fit_lines: { rule: string; points: number }[];
  messages: LeadMessage[];
  unverified_contacts: { channel: string; value: string }[];
};

export type SendOutcome = { status: "sent" | "open_in_app" | "failed"; message_id: string; link: string | null; detail?: string };
