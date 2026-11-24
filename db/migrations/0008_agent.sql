-- The agent: chat cases that start before an item is known, a record of every model
-- attempt, and a separate home for workflow checkpoints.

-- A chat case can open with nothing but the shopper's words. The order line, quantity,
-- action and reason are filled in as the conversation establishes them.
alter table cases alter column order_id drop not null;
alter table cases alter column order_line_id drop not null;
alter table cases alter column quantity drop not null;
alter table cases alter column requested_action drop not null;
alter table cases alter column reason_code drop not null;

-- 'answered' covers cases that end with information only: a cited policy answer or a status.
alter table cases drop constraint cases_outcome_type_check;
alter table cases add constraint cases_outcome_type_check check (
  outcome_type in (
    'refund', 'declined_ineligible', 'declined_by_supervisor', 'payment_failed', 'answered'
  )
);

-- How the case came in: one typed request, or a conversation run by the agent.
alter table cases add column channel text not null default 'typed'
  check (channel in ('typed', 'chat'));

-- Every model attempt, including failures and retries. This is what cost and latency
-- reporting is built on; it is written by the API in the caller's own transaction scope.
create table model_attempts (
  id bigint generated always as identity primary key,
  merchant_id uuid not null,
  case_id uuid not null,
  turn integer not null,
  purpose text not null check (purpose in ('interpret', 'answer')),
  -- The upstream service that served the attempt and the model it reported.
  route text not null,
  model text not null,
  tier text not null check (tier in ('low', 'strong')),
  outcome text not null check (
    outcome in ('ok', 'invalid_output', 'timeout', 'rate_limited', 'error', 'budget_exhausted')
  ),
  input_tokens integer,
  output_tokens integer,
  latency_ms integer,
  error text,
  request_id text,
  created_at timestamptz not null,
  foreign key (merchant_id, case_id) references cases (merchant_id, id)
);

create index model_attempts_case on model_attempts (case_id, id);

alter table model_attempts enable row level security;

grant select, insert on model_attempts to rd_app;
grant select, insert, update, delete on model_attempts to rd_service;

create policy model_attempts_select on model_attempts for select to rd_app
  using (merchant_id = app_merchant() and app_is_staff());

create policy model_attempts_insert on model_attempts for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = model_attempts.case_id)
  );

-- Workflow checkpoints (LangGraph) live in their own schema under their own role. That role
-- can reach nothing else: no orders, no cases, no money. The tables themselves are created
-- by the checkpointer library when migrations run. A thread is only ever loaded after the
-- caller's access to its case has been checked under row-level security, and thread IDs
-- carry the merchant and case IDs.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'rd_workflow') then
    create role rd_workflow nologin;
  end if;
end $$;

create schema workflow;
revoke all on schema workflow from public;
grant usage on schema workflow to rd_workflow;
