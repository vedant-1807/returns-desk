-- Payment provider events (webhooks).
--
-- A provider tells us about a refund more than once, late, and in any order. Every
-- authentic event is stored exactly once, keyed by the provider's own event ID, so a
-- repeat is recognised before it can do anything. What an event may change is decided by
-- the execution service: it can settle an action that is still open, and it can never
-- overwrite a result that is already final.
--
-- Not tenant data the API serves: only the execution service reads and writes it.

create table payment_events (
  id bigint generated always as identity primary key,
  provider text not null,
  -- The provider's own ID for this event. Deliveries of the same event share it.
  event_id text not null,
  event_type text not null,
  -- Filled in when the event could be tied to one of our actions.
  merchant_id uuid,
  action_id uuid,
  provider_refund_id text,
  refund_status text,
  -- When the provider says it happened, which is not when it arrived.
  occurred_at timestamptz,
  payload jsonb not null,
  -- What we did with it: settled, settled_failure, already_final, conflicts_with_final,
  -- not_final_yet, unknown_action, account_mismatch, receipt_mismatch, ignored_type.
  outcome text not null,
  received_at timestamptz not null,
  unique (provider, event_id)
);

create index payment_events_action on payment_events (action_id);

alter table payment_events enable row level security;
grant select, insert, update on payment_events to rd_service;

-- The provider account a merchant's payments run under. Events carry it, and an event is
-- only applied to an action of the merchant it names.
alter table merchants add column payment_account text unique;

-- An event can settle an action, so it appears in the action's attempt history too.
alter table action_attempts drop constraint action_attempts_operation_check;
alter table action_attempts add constraint action_attempts_operation_check
  check (operation in ('create_refund', 'find_refund', 'provider_event'));
