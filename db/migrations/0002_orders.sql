-- Orders and order lines. Amounts are integer cents, per unit, after discounts.
-- Line tax and rounding allocation are deferred to v2, so a line stores the paid amount
-- of one unit and every unit on a line is worth the same.

create table orders (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references merchants (id),
  shopper_id uuid not null,
  order_number text not null,
  purchased_at timestamptz not null,
  delivered_at timestamptz,
  currency text not null default 'USD' check (currency = 'USD'),
  provider_payment_ref text not null unique,
  created_at timestamptz not null default now(),
  unique (merchant_id, id),
  unique (merchant_id, order_number),
  -- Tenant-scoped foreign key: an order can only point at a shopper of the same merchant.
  foreign key (merchant_id, shopper_id) references shoppers (merchant_id, id)
);

create table order_lines (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null,
  order_id uuid not null,
  sku text not null,
  variant text not null default '',
  description text not null,
  quantity integer not null check (quantity > 0),
  unit_paid_cents integer not null check (unit_paid_cents >= 0),
  -- Units and cents held by an authorized action that has not settled yet.
  reserved_quantity integer not null default 0 check (reserved_quantity >= 0),
  reserved_cents bigint not null default 0 check (reserved_cents >= 0),
  -- Units and cents confirmed refunded by the processor.
  refunded_quantity integer not null default 0 check (refunded_quantity >= 0),
  refunded_cents bigint not null default 0 check (refunded_cents >= 0),
  unique (merchant_id, id),
  unique (order_id, id),
  foreign key (merchant_id, order_id) references orders (merchant_id, id),
  -- The remaining-balance rule lives in the database: pending plus completed actions can
  -- never exceed what was bought, whatever the application code does.
  constraint order_lines_quantity_balance
    check (reserved_quantity + refunded_quantity <= quantity),
  constraint order_lines_cents_balance
    check (reserved_cents + refunded_cents <= quantity::bigint * unit_paid_cents)
);

create index order_lines_order on order_lines (order_id);

alter table orders enable row level security;
alter table order_lines enable row level security;

grant select on orders to rd_app;
grant select on order_lines to rd_app;
-- The API may only move the reservation counters, and only inside the caller's merchant.
grant update (reserved_quantity, reserved_cents) on order_lines to rd_app;

create policy orders_select on orders for select to rd_app
  using (
    merchant_id = app_merchant()
    and (app_is_staff() or shopper_id = app_shopper())
  );

-- A line is visible exactly when its order is visible (the subquery runs under orders' RLS).
create policy order_lines_select on order_lines for select to rd_app
  using (
    merchant_id = app_merchant()
    and exists (
      select 1 from orders o
      where o.merchant_id = order_lines.merchant_id and o.id = order_lines.order_id
    )
  );

create policy order_lines_update on order_lines for update to rd_app
  using (
    merchant_id = app_merchant()
    and exists (
      select 1 from orders o
      where o.merchant_id = order_lines.merchant_id and o.id = order_lines.order_id
    )
  )
  with check (merchant_id = app_merchant());
