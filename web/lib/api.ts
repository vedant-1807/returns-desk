// A small client for the Returns Desk API. The token is kept for the browser tab only.

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Role = "shopper" | "associate" | "supervisor";

export type Session = {
  token: string;
  role: Role;
  merchant: string;
  merchantName: string;
  name: string;
  email: string;
};

export type DevUser = {
  email: string;
  display_name: string;
  merchant: string;
  merchant_name: string;
  role: Role;
};

export type Message = { author_kind: string; body: string; created_at: string };

export type Citation = {
  version_label: string;
  section_key: string;
  title: string;
  body: string;
};

export type CaseView = {
  id: string;
  state: string;
  status: string;
  status_message: string;
  next_action: string | null;
  item: {
    order_number: string | null;
    description: string | null;
    variant: string | null;
    quantity: number | null;
  };
  amount_cents: number | null;
  policy_citations: Citation[];
  receipt: { refund_reference: string; amount_cents: number; confirmed_at: string } | null;
  outcome: { type: string; reason: string | null } | null;
  messages: Message[];
  requested_at: string;
  channel: string;
  // Staff only.
  reason_code?: string | null;
  proposal?: { id: string; version: number; status: string; decision: string } | null;
  decision?: {
    decision: string;
    reviewer_user_id: string;
    proposal_version: number;
    note: string;
    decided_at: string;
  } | null;
  // Supervisors only.
  review?: { reasons: string[]; internal_evidence: Record<string, unknown> } | null;
};

export type CaseSummary = {
  id: string;
  state: string;
  description: string | null;
  variant: string | null;
  order_number: string | null;
  updated_at: string;
};

export type OrderLine = {
  id: string;
  description: string;
  variant: string;
  quantity: number;
  unit_paid_cents: number;
  remaining_quantity: number;
  remaining_cents: number;
};

export type Order = {
  id: string;
  order_number: string;
  purchased_at: string;
  delivered_at: string | null;
  lines: OrderLine[];
};

export type Review = {
  review_id: string;
  proposal_version: number;
  case_id: string;
  proposed_action: string;
  item: { order_number: string; description: string; variant: string; quantity: number };
  amount_cents: number;
  request_reason: string;
  requested_at: string;
  reasons: string[];
  internal_evidence: Record<string, unknown>;
  applicable_policy: Citation[];
  internal_rule: {
    version_label: string;
    rules: Record<string, unknown>;
    sections: { section_key: string; title: string; body: string }[];
  } | null;
};

const KEY = "returns-desk-session";

export function loadSession(): Session | null {
  if (typeof window === "undefined") return null;
  const raw = window.sessionStorage.getItem(KEY);
  return raw ? (JSON.parse(raw) as Session) : null;
}

export function saveSession(session: Session | null) {
  if (session) window.sessionStorage.setItem(KEY, JSON.stringify(session));
  else window.sessionStorage.removeItem(KEY);
}

export class ApiError extends Error {
  status: number;
  code: string;
  details?: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export async function api<T>(
  path: string,
  options: { method?: "GET" | "POST"; body?: unknown; token?: string } = {},
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "content-type": "application/json",
      ...(options.token ? { authorization: `Bearer ${options.token}` } : {}),
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = data?.error ?? {};
    throw new ApiError(
      response.status,
      error.code ?? "error",
      error.message ?? `Request failed (${response.status})`,
      error.details,
    );
  }
  return data as T;
}

export function money(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "";
  return `$${(cents / 100).toFixed(2)}`;
}

export function requestId(): string {
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function itemLabel(item: {
  description: string | null;
  variant: string | null;
  quantity?: number | null;
}): string {
  if (!item.description) return "No item chosen yet";
  const name = item.variant ? `${item.description} (${item.variant})` : item.description;
  return item.quantity && item.quantity !== 1 ? `${item.quantity} x ${name}` : name;
}

export const STATE_LABEL: Record<string, string> = {
  open: "Open",
  awaiting_customer: "Waiting for shopper",
  awaiting_supervisor: "Waiting for supervisor",
  processing: "Processing",
  reconciling: "Pending verification",
  resolved: "Resolved",
  declined: "Declined",
};

export const CUSTOMER_STATUS_LABEL: Record<string, string> = {
  received: "Received",
  needs_your_reply: "Needs your reply",
  needs_your_confirmation: "Needs your confirmation",
  in_review: "In review",
  processing: "Processing",
  pending_verification: "Pending verification",
  refunded: "Refunded",
  answered: "Answered",
  declined: "Declined",
};

export const REASON_LABEL: Record<string, string> = {
  above_auto_limit: "The amount is above the automatic limit",
  return_history_threshold: "Three or more returns in the last 30 days",
  reason_requires_review: "The reason given needs a person to check",
  delivery_unconfirmed: "Delivery is not confirmed",
  policy_needs_verification: "No single policy version applies",
};
