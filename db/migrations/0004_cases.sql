-- Cases, proposals, the restricted review record and supervisor decisions.

create type case_state as enum (
  'open',
  'awaiting_customer',
  'awaiting_supervisor',
  'processing',
  'reconciling',
  'resolved',
  'declined'
);

create table cases (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references merchants (id),
  shopper_id uuid not null,
  -- Sent by the client so a refresh or double-submit maps back to the same case.
  client_request_id text not null,
  order_id uuid not null,
  order_line_id uuid not null,
  quantity integer not null check (quantity > 0),
  requested_action text not null check (requested_action in ('refund', 'exchange')),
  reason_code text not null,
  state case_state not null default 'open',
  outcome_type text check (
    outcome_type in ('refund', 'declined_ineligible', 'declined_by_supervisor', 'payment_failed')
  ),
  outcome_reason text,
  -- The original request time. Return windows are always judged against this moment,
  -- however long clarification or review takes afterwards.
  request_received_at timestamptz not null,
  resolved_at timestamptz,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  unique (merchant_id, id),
  unique (merchant_id, shopper_id, client_request_id),
  foreign key (merchant_id, shopper_id) references shoppers (merchant_id, id),
  foreign key (merchant_id, order_id) references orders (merchant_id, id),
  foreign key (merchant_id, order_line_id) references order_lines (merchant_id, id),
  foreign key (order_id, order_line_id) references order_lines (order_id, id)
);

create index cases_line on cases (order_line_id);
create index cases_shopper_resolved on cases (merchant_id, shopper_id, resolved_at);

create table case_messages (
  id uuid primary key default gen_random_uuid(),
  -- Conversation order. Several messages can share a timestamp (a shopper's message and the
  -- system's reply are written in one transaction), so order by this, never by time.
  seq bigint generated always as identity,
  merchant_id uuid not null,
  case_id uuid not null,
  author_kind text not null check (author_kind in ('shopper', 'system', 'associate', 'supervisor')),
  author_user_id uuid references app_users (id),
  audience text not null default 'customer' check (audience in ('customer', 'internal')),
  body text not null,
  client_request_id text,
  created_at timestamptz not null,
  foreign key (merchant_id, case_id) references cases (merchant_id, id)
);

create unique index case_messages_client_request
  on case_messages (case_id, client_request_id)
  where client_request_id is not null;

-- Append-only audit trail. Every state change records who, when and which HTTP request.
create table case_events (
  id bigint generated always as identity primary key,
  merchant_id uuid not null,
  case_id uuid not null,
  kind text not null,
  from_state case_state,
  to_state case_state,
  actor_kind text not null,
  actor_user_id uuid,
  request_id text,
  detail jsonb not null default '{}',
  created_at timestamptz not null,
  foreign key (merchant_id, case_id) references cases (merchant_id, id)
);

create index case_events_case on case_events (case_id, id);

-- One row is one version of a case's proposal. If the item, amount or governing rules
-- change, a new version is written and needs fresh consent or approval.
create table proposals (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null,
  case_id uuid not null,
  version integer not null check (version > 0),
  -- Assigned as soon as a specific resolution is proposed. It becomes the action's ID and
  -- the root of its idempotency key, so one proposal can never pay out twice.
  action_id uuid not null unique default gen_random_uuid(),
  action_kind text not null check (action_kind in ('refund')),
  order_line_id uuid not null,
  quantity integer not null check (quantity > 0),
  amount_cents bigint not null check (amount_cents >= 0),
  decision text not null check (decision in ('auto', 'review', 'ineligible')),
  -- Customer-safe outcome codes only. Internal review reasons live in review_records.
  reason_codes text[] not null default '{}',
  customer_policy_version_id uuid,
  cited_sections text[] not null default '{}',
  operational_policy_version_id uuid,
  evidence jsonb not null default '{}',
  status text not null check (
    status in (
      'awaiting_consent', 'pending_review', 'approved', 'declined',
      'submitted', 'ineligible', 'superseded'
    )
  ),
  consented_at timestamptz,
  created_at timestamptz not null,
  unique (merchant_id, id),
  unique (case_id, version),
  foreign key (merchant_id, case_id) references cases (merchant_id, id),
  foreign key (merchant_id, order_line_id) references order_lines (merchant_id, id)
);

-- Why a proposal needs a human, plus the internal evidence (approval limit, return history).
-- Supervisors only: the shopper and the associate never see these rows.
create table review_records (
  proposal_id uuid primary key,
  merchant_id uuid not null,
  reasons text[] not null,
  internal_evidence jsonb not null default '{}',
  created_at timestamptz not null,
  foreign key (merchant_id, proposal_id) references proposals (merchant_id, id)
);

-- At most one final decision per proposal version: proposal_id is unique.
create table approvals (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null,
  proposal_id uuid not null unique,
  proposal_version integer not null,
  reviewer_user_id uuid not null references app_users (id),
  decision text not null check (decision in ('approved', 'declined')),
  note text not null default '',
  request_id text,
  decided_at timestamptz not null,
  foreign key (merchant_id, proposal_id) references proposals (merchant_id, id)
);

alter table cases enable row level security;
alter table case_messages enable row level security;
alter table case_events enable row level security;
alter table proposals enable row level security;
alter table review_records enable row level security;
alter table approvals enable row level security;

grant select, insert, update on cases, proposals to rd_app;
grant select, insert on case_messages, case_events, review_records, approvals to rd_app;

-- Cases: staff see their merchant's cases, a shopper sees only their own.
create policy cases_select on cases for select to rd_app
  using (
    merchant_id = app_merchant()
    and (app_is_staff() or shopper_id = app_shopper())
  );

create policy cases_insert on cases for insert to rd_app
  with check (merchant_id = app_merchant() and shopper_id = app_shopper());

create policy cases_update on cases for update to rd_app
  using (
    merchant_id = app_merchant()
    and (app_is_staff() or shopper_id = app_shopper())
  )
  with check (merchant_id = app_merchant());

-- Everything below hangs off case visibility (the subqueries run under cases' RLS).
create policy case_messages_select on case_messages for select to rd_app
  using (
    merchant_id = app_merchant()
    and (audience = 'customer' or app_is_staff())
    and exists (select 1 from cases c where c.id = case_messages.case_id)
  );

create policy case_messages_insert on case_messages for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = case_messages.case_id)
  );

create policy case_events_select on case_events for select to rd_app
  using (merchant_id = app_merchant() and app_is_staff());

create policy case_events_insert on case_events for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = case_events.case_id)
  );

create policy proposals_select on proposals for select to rd_app
  using (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = proposals.case_id)
  );

create policy proposals_insert on proposals for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = proposals.case_id)
  );

create policy proposals_update on proposals for update to rd_app
  using (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = proposals.case_id)
  )
  with check (merchant_id = app_merchant());

create policy review_records_select on review_records for select to rd_app
  using (merchant_id = app_merchant() and app_role() = 'supervisor');

create policy review_records_insert on review_records for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from proposals p where p.id = review_records.proposal_id)
  );

create policy approvals_select on approvals for select to rd_app
  using (merchant_id = app_merchant() and app_is_staff());

-- Only a supervisor can record a decision, and only under their own name.
create policy approvals_insert on approvals for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and app_role() = 'supervisor'
    and reviewer_user_id = app_user()
  );
