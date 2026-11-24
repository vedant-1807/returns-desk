-- Model budgets per merchant, one correlation ID from request to refund, an associate's
-- request for review, and recorded eval runs for the operations view.

-- A merchant's daily model budget in tokens (input plus output). Null means the service
-- default applies (RD_MODEL_DAILY_TOKEN_CAP, where 0 is "no cap").
alter table merchants add column daily_token_cap bigint
  check (daily_token_cap is null or daily_token_cap >= 0);

-- Tokens the caller's merchant has used since a moment in time, and the merchant's own cap.
-- A shopper's turn must be held to the merchant's budget, but a shopper may not read other
-- shoppers' model attempts. So this runs with the owner's rights and returns two numbers
-- about the caller's own merchant and nothing else.
create function model_budget_status(p_since timestamptz)
  returns table (tokens_used bigint, daily_token_cap bigint)
  language sql stable security definer
  set search_path = public, pg_temp
  as $$
    select
      coalesce(
        (select sum(coalesce(a.input_tokens, 0) + coalesce(a.output_tokens, 0))
         from model_attempts a
         where a.merchant_id = app_merchant() and a.created_at >= p_since),
        0
      )::bigint,
      (select m.daily_token_cap from merchants m where m.id = app_merchant())
  $$;

revoke all on function model_budget_status(timestamptz) from public;
grant execute on function model_budget_status(timestamptz) to rd_app, rd_service;

-- The request that authorized an action. It is sent to the payment processor as metadata,
-- so one correlation ID links the shopper's request, the agent run, the approval and the
-- refund on the processor's side.
alter table resolution_actions add column request_id text;

-- An associate can ask for a supervisor's review of a case. From then on the rules treat
-- the case as a review case, whatever the amount. Who asked, when and why are also kept
-- as an audit event and an internal note.
alter table cases add column review_requested_at timestamptz;
alter table cases add column review_requested_by uuid references app_users (id);

-- Summaries of eval runs, recorded by the eval CLI so the operations view can show how the
-- agent did on evaluated cases. This is not tenant data: the suite runs in its own
-- database against the demo merchants and reports per merchant by slug.
create table eval_runs (
  id uuid primary key default gen_random_uuid(),
  suite text not null,
  label text not null,
  seed integer not null,
  cases integer not null check (cases >= 0),
  metrics jsonb not null,
  by_merchant jsonb not null default '{}',
  recorded_at timestamptz not null default now()
);

create index eval_runs_recorded on eval_runs (recorded_at desc);

alter table eval_runs enable row level security;

grant select on eval_runs to rd_app;
grant select, insert, update, delete on eval_runs to rd_service;

create policy eval_runs_select on eval_runs for select to rd_app
  using (app_is_staff());
