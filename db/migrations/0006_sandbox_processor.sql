-- The sandbox payment processor. It plays the part of an outside provider (Stripe test mode
-- takes over in the stretch milestone), so it keeps its own records in its own schema under
-- its own role. The application roles cannot read or write here, and this role cannot read
-- application tables. Tests compare the two sides independently.

create schema sandbox_processor;

create table sandbox_processor.payments (
  payment_ref text primary key,
  account text not null,
  amount_cents bigint not null check (amount_cents > 0),
  currency text not null default 'USD',
  created_at timestamptz not null default now()
);

create table sandbox_processor.refunds (
  id text primary key,
  payment_ref text not null references sandbox_processor.payments (payment_ref),
  amount_cents bigint not null check (amount_cents > 0),
  currency text not null default 'USD',
  status text not null check (status in ('succeeded')),
  idempotency_key text not null unique,
  metadata jsonb not null default '{}',
  created_at timestamptz not null
);

create index sandbox_refunds_payment on sandbox_processor.refunds (payment_ref);

-- Every call the application makes, including ones that end in an injected fault.
-- "No provider call" in a test means no row here.
create table sandbox_processor.requests (
  id bigint generated always as identity primary key,
  operation text not null,
  payment_ref text,
  idempotency_key text,
  outcome text not null,
  fault text,
  at timestamptz not null
);

-- Armed faults, consumed oldest first by create_refund calls.
--   lost_response   the refund is recorded, then the response never arrives
--   timeout_before  the call times out before anything is recorded
--   reject          the provider refuses the refund (a verified failure)
create table sandbox_processor.faults (
  id bigint generated always as identity primary key,
  kind text not null check (kind in ('lost_response', 'timeout_before', 'reject')),
  payment_ref text,
  remaining integer not null check (remaining >= 0),
  created_at timestamptz not null default now()
);

revoke all on schema sandbox_processor from public;
grant usage on schema sandbox_processor to rd_processor;
grant select, insert, update, delete on all tables in schema sandbox_processor to rd_processor;
