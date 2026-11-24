-- Authorized actions, the execution queue (outbox), attempt history and the payment ledger.
--
-- The API (rd_app) can create an action and queue it, in the same transaction that reserves
-- the line. It cannot settle one: only the execution service (rd_service) may record a
-- processor result or write the ledger.

create type action_status as enum ('pending', 'reconciling', 'succeeded', 'failed', 'cancelled');

create table resolution_actions (
  -- Same value as proposals.action_id.
  id uuid primary key references proposals (action_id),
  merchant_id uuid not null,
  case_id uuid not null,
  proposal_id uuid not null unique,
  kind text not null check (kind in ('refund')),
  order_id uuid not null,
  order_line_id uuid not null,
  quantity integer not null check (quantity > 0),
  amount_cents bigint not null check (amount_cents > 0),
  currency text not null default 'USD' check (currency = 'USD'),
  provider_payment_ref text not null,
  -- One logical refund keeps one key for its whole life, across retries and restarts.
  idempotency_key text not null unique,
  provider_refund_id text unique,
  status action_status not null default 'pending',
  attempt_count integer not null default 0,
  last_error text,
  -- The operational rules version rechecked when the action was authorized.
  operational_policy_version_id uuid,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  completed_at timestamptz,
  unique (merchant_id, id),
  foreign key (merchant_id, case_id) references cases (merchant_id, id),
  foreign key (merchant_id, proposal_id) references proposals (merchant_id, id),
  foreign key (merchant_id, order_line_id) references order_lines (merchant_id, id)
);

create index resolution_actions_line on resolution_actions (order_line_id);
create index resolution_actions_case on resolution_actions (case_id);

-- Outbox: written in the same transaction as the reservation and the action, so an
-- authorized refund can never be lost between "decided" and "sent".
create table outbox (
  id bigint generated always as identity primary key,
  merchant_id uuid not null,
  action_id uuid not null unique references resolution_actions (id),
  topic text not null default 'execute_refund',
  available_at timestamptz not null,
  lease_until timestamptz,
  lease_owner text,
  attempts integer not null default 0,
  done_at timestamptz,
  created_at timestamptz not null
);

create index outbox_due on outbox (available_at) where done_at is null;

create table action_attempts (
  id bigint generated always as identity primary key,
  merchant_id uuid not null,
  action_id uuid not null references resolution_actions (id),
  attempt_no integer not null,
  operation text not null check (operation in ('create_refund', 'find_refund')),
  outcome text not null check (outcome in ('succeeded', 'found', 'not_found', 'unknown', 'rejected')),
  detail jsonb not null default '{}',
  worker text not null,
  at timestamptz not null
);

create index action_attempts_action on action_attempts (action_id, id);

-- The application's payment ledger. One row per confirmed refund, never more:
-- action_id and provider_refund_id are both unique.
create table ledger_entries (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null,
  action_id uuid not null unique references resolution_actions (id),
  order_id uuid not null,
  order_line_id uuid not null,
  kind text not null check (kind in ('refund')),
  quantity integer not null check (quantity > 0),
  amount_cents bigint not null check (amount_cents > 0),
  provider_refund_id text not null unique,
  recorded_at timestamptz not null,
  foreign key (merchant_id, order_line_id) references order_lines (merchant_id, id)
);

create index ledger_entries_line on ledger_entries (order_line_id);

alter table resolution_actions enable row level security;
alter table outbox enable row level security;
alter table action_attempts enable row level security;
alter table ledger_entries enable row level security;

grant select, insert on resolution_actions to rd_app;
grant insert on outbox to rd_app;
grant select on action_attempts, ledger_entries to rd_app;

create policy resolution_actions_select on resolution_actions for select to rd_app
  using (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = resolution_actions.case_id)
  );

create policy resolution_actions_insert on resolution_actions for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = resolution_actions.case_id)
  );

create policy outbox_insert on outbox for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from resolution_actions a where a.id = outbox.action_id)
  );

create policy action_attempts_select on action_attempts for select to rd_app
  using (merchant_id = app_merchant() and app_is_staff());

create policy ledger_entries_select on ledger_entries for select to rd_app
  using (
    merchant_id = app_merchant()
    and exists (select 1 from resolution_actions a where a.id = ledger_entries.action_id)
  );

-- The execution service works across merchants. It gets full access to application tables;
-- this grant is last so it covers every table created so far.
grant select, insert, update, delete on all tables in schema public to rd_service;
