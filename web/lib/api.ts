// A small client for the Returns Desk API. The session is kept for the browser tab only.

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Set both to sign in through Supabase Auth. Left empty, the app offers the local demo
// sign-in instead, which only exists when the API runs with RD_ENVIRONMENT=local.
export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
export const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";

export type Role = "shopper" | "associate" | "supervisor";
export type Surface = "shopper" | "staff";

export type Session = {
  token: string;
  role: Role;
  // Which of the user's own relationships this tab is using. The API checks both against
  // its database on every request; they choose, they do not grant.
  merchant: string;
  surface: Surface;
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

export type Membership = { merchant_slug: string; merchant_name: string; role: Role };

export type Message = {
  author_kind: string;
  // "internal" notes are only ever sent to staff.
  audience: string;
  body: string;
  created_at: string;
};

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
  requested_action: string | null;
  item: {
    order_number: string | null;
    description: string | null;
    variant: string | null;
    quantity: number | null;
  };
  // Money that is or would be refunded. Null for an exchange, which moves none.
  amount_cents: number | null;
  exchange: {
    replacement_variant: string | null;
    fulfillment: { reference: string; variant: string; quantity: number } | null;
  } | null;
  policy_citations: Citation[];
  receipt: { refund_reference: string; amount_cents: number; confirmed_at: string } | null;
  outcome: { type: string; reason: string | null } | null;
  messages: Message[];
  requested_at: string;
  channel: string;
  // Staff only.
  reason_code?: string | null;
  review_requested?: { at: string; by: string } | null;
  trace?: { trace_id: string; path: string; langfuse_url: string | null };
  proposal?: {
    id: string;
    version: number;
    status: string;
    decision: string;
    action_kind: string;
    value_cents: number;
  } | null;
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
  requested_action: string | null;
  replacement_variant: string | null;
  description: string | null;
  variant: string | null;
  order_number: string | null;
  updated_at: string;
  review_requested: boolean;
  needs_staff: boolean;
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
  replacement_variant: string | null;
  item: { order_number: string; description: string; variant: string; quantity: number };
  // The paid value of the units. For an exchange no money moves.
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

export type TraceSpan = {
  span_id: string;
  parent_id: string | null;
  kind: string;
  name: string;
  start: string;
  ms: number;
  level: string;
  status_message: string | null;
  attributes: Record<string, unknown>;
};

export type Trace = {
  trace_id: string;
  request_ids: string[];
  langfuse_url: string | null;
  totals: {
    active_ms: number;
    human_wait_seconds: Record<string, number>;
    model_attempts: number;
    model_attempts_not_ok: number;
    input_tokens: number;
    output_tokens: number;
    list_price_usd: number;
    unpriced_attempts: number;
  };
  spans: TraceSpan[];
  review?: { proposal_version: number; reasons: string[] }[];
};

export type PolicyVersion = {
  id: string;
  version_label: string;
  effective_from: string;
  effective_to: string | null;
  rules: Record<string, unknown>;
  sections: { section_key: string; title: string; body: string; audience: string }[];
};

export type Policy = {
  id: string;
  policy_type: string;
  scope: string;
  name: string;
  versions: PolicyVersion[];
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

export function surfaceFor(role: Role): Surface {
  return role === "shopper" ? "shopper" : "staff";
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
  options: {
    method?: "GET" | "POST";
    body?: unknown;
    // A signed-in session: sends the token and which merchant and surface it is for.
    session?: Session | null;
    // A bare token, for the one call made before a merchant has been chosen.
    token?: string;
  } = {},
): Promise<T> {
  const token = options.session?.token ?? options.token;
  const response = await fetch(`${API_URL}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(options.session
        ? { "x-rd-merchant": options.session.merchant, "x-rd-surface": options.session.surface }
        : {}),
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

/** Exchange an email and password for an access token at Supabase Auth. */
export async function supabaseSignIn(email: string, password: string): Promise<string> {
  const response = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { "content-type": "application/json", apikey: SUPABASE_ANON_KEY },
    body: JSON.stringify({ email, password }),
    cache: "no-store",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.access_token) {
    throw new Error(data.error_description ?? data.msg ?? "The email or password is not right");
  }
  return data.access_token as string;
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
  exchanged: "Exchanged",
  answered: "Answered",
  declined: "Declined",
};

export const REASON_LABEL: Record<string, string> = {
  above_auto_limit: "The value is above the automatic limit",
  return_history_threshold: "Three or more returns in the last 30 days",
  reason_requires_review: "The reason given needs a person to check",
  delivery_unconfirmed: "Delivery is not confirmed",
  policy_needs_verification: "No single policy version applies",
  staff_escalation: "An associate asked for a review",
};
