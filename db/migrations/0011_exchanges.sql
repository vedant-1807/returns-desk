-- Exchanges and inventory.
--
-- An exchange swaps units of an order line for a variant of the same product. No money
-- moves. In one transaction it takes the replacement out of stock, uses up the line's
-- returnable units and writes one fulfillment record. The rules that make that safe live
-- in the database, like the refund balance does:
--
--   * stock can never go below zero (inventory.available >= 0),
--   * a unit can be refunded, held for a refund, or exchanged, but only one of the three
--     (the order line balance check now counts exchanged units too),
--   * one proposal produces at most one action, so at most one fulfillment.

create table inventory (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references merchants (id),
  sku text not null,
  variant text not null,
  description text not null,
  -- Units that can still be promised. An exchange lowers this in the same transaction
  -- that records its fulfillment.
  available integer not null check (available >= 0),
  unique (merchant_id, sku, variant)
);

-- A unit is refunded, held for a pending refund, or exchanged. Never two of those.
alter table order_lines
  add column exchanged_quantity integer not null default 0 check (exchanged_quantity >= 0);

alter table order_lines drop constraint order_lines_quantity_balance;
alter table order_lines add constraint order_lines_quantity_balance
  check (reserved_quantity + refunded_quantity + exchanged_quantity <= quantity);

-- Money can only be refunded for units that were not exchanged.
alter table order_lines drop constraint order_lines_cents_balance;
alter table order_lines add constraint order_lines_cents_balance
  check (
    reserved_cents + refunded_cents <= (quantity - exchanged_quantity)::bigint * unit_paid_cents
  );

grant update (exchanged_quantity) on order_lines to rd_app;

-- What the shopper wants instead, once the conversation has established it.
alter table cases add column replacement_variant text;

alter table cases drop constraint cases_outcome_type_check;
alter table cases add constraint cases_outcome_type_check check (
  outcome_type in (
    'refund', 'exchange', 'declined_ineligible', 'declined_by_supervisor', 'payment_failed',
    'answered'
  )
);

alter table proposals drop constraint proposals_action_kind_check;
alter table proposals add constraint proposals_action_kind_check
  check (action_kind in ('refund', 'exchange'));
alter table proposals add column replacement_sku text;
alter table proposals add column replacement_variant text;
alter table proposals add constraint proposals_replacement_for_exchange
  check ((action_kind = 'exchange') = (replacement_variant is not null));

alter table resolution_actions drop constraint resolution_actions_kind_check;
alter table resolution_actions add constraint resolution_actions_kind_check
  check (kind in ('refund', 'exchange'));
-- A refund moves money; an exchange moves none.
alter table resolution_actions drop constraint resolution_actions_amount_cents_check;
alter table resolution_actions add constraint resolution_actions_amount_cents_check
  check ((kind = 'refund' and amount_cents > 0) or (kind = 'exchange' and amount_cents = 0));

-- The API role may record an exchange as done, because the whole exchange happens inside
-- its own transaction. A refund it may only queue: settling one stays with the execution
-- service, which is the only code that talks to the payment processor.
drop policy resolution_actions_insert on resolution_actions;
create policy resolution_actions_insert on resolution_actions for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = resolution_actions.case_id)
    and (kind = 'exchange' or status = 'pending')
  );

-- The replacement promised to the shopper: the sandbox stand-in for a warehouse order.
-- One per action, so one per proposal.
create table fulfillments (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null,
  action_id uuid not null unique references resolution_actions (id),
  case_id uuid not null,
  order_id uuid not null,
  order_line_id uuid not null,
  sku text not null,
  variant text not null,
  quantity integer not null check (quantity > 0),
  -- The reference shown to the shopper.
  reference text not null unique,
  status text not null default 'reserved' check (status in ('reserved')),
  created_at timestamptz not null,
  foreign key (merchant_id, case_id) references cases (merchant_id, id),
  foreign key (merchant_id, order_line_id) references order_lines (merchant_id, id)
);

create index fulfillments_case on fulfillments (case_id);

alter table inventory enable row level security;
alter table fulfillments enable row level security;

grant select on inventory to rd_app;
-- The API may only lower or raise the stock count, and only inside the caller's merchant.
grant update (available) on inventory to rd_app;
grant select, insert on fulfillments to rd_app;
grant select, insert, update, delete on inventory, fulfillments to rd_service;

-- Stock levels are catalogue information: anyone signed in to the merchant may read them.
create policy inventory_select on inventory for select to rd_app
  using (merchant_id = app_merchant());

create policy inventory_update on inventory for update to rd_app
  using (merchant_id = app_merchant())
  with check (merchant_id = app_merchant());

-- A fulfillment is visible exactly when its case is (the subquery runs under cases' RLS).
create policy fulfillments_select on fulfillments for select to rd_app
  using (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = fulfillments.case_id)
  );

create policy fulfillments_insert on fulfillments for insert to rd_app
  with check (
    merchant_id = app_merchant()
    and exists (select 1 from cases c where c.id = fulfillments.case_id)
  );
