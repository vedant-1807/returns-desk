-- Tenancy layer: merchants, staff memberships, shoppers, per-request claims and
-- the row-level security helpers every later table builds on.

create extension if not exists btree_gist;

-- Roles. Passwords are set by the migration runner from the environment, never here.
--   rd_app        the API at request time; always subject to row-level security
--   rd_service    admin jobs only (refund execution, reconciliation); bypasses RLS
--   rd_processor  the sandbox payment processor; cannot read application tables
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'rd_app') then
    create role rd_app nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'rd_service') then
    create role rd_service nologin bypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'rd_processor') then
    create role rd_processor nologin;
  end if;
end $$;

grant usage on schema public to rd_app, rd_service;

create table merchants (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  name text not null,
  timezone text not null,
  currency text not null default 'USD' check (currency = 'USD'),
  created_at timestamptz not null default now()
);

-- Stands in for Supabase auth.users until the deploy milestone.
create table app_users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  display_name text not null,
  created_at timestamptz not null default now()
);

create type staff_role as enum ('associate', 'supervisor');

create table memberships (
  merchant_id uuid not null references merchants (id),
  user_id uuid not null references app_users (id),
  role staff_role not null,
  created_at timestamptz not null default now(),
  primary key (merchant_id, user_id)
);

-- v1 rule: a staff user belongs to one merchant. Drop this index for v2 workspace switching.
create unique index memberships_one_merchant_per_user on memberships (user_id);

-- A shopper relationship is separate from staff membership: owning an order is its own check.
create table shoppers (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references merchants (id),
  user_id uuid not null references app_users (id),
  display_name text not null,
  created_at timestamptz not null default now(),
  unique (merchant_id, user_id),
  unique (merchant_id, id)
);

-- Per-request claims. The API sets these with set_config(..., true) at the start of each
-- transaction, so they vanish at commit and never leak across pooled connections.
create function app_merchant() returns uuid
  language sql stable
  as $$ select nullif(current_setting('app.merchant_id', true), '')::uuid $$;

create function app_user() returns uuid
  language sql stable
  as $$ select nullif(current_setting('app.user_id', true), '')::uuid $$;

-- 'shopper', 'associate' or 'supervisor'. Null when no claims are set, which matches no policy.
create function app_role() returns text
  language sql stable
  as $$ select nullif(current_setting('app.role', true), '') $$;

-- The caller's shopper record at this merchant. Null for staff.
create function app_shopper() returns uuid
  language sql stable
  as $$ select nullif(current_setting('app.shopper_id', true), '')::uuid $$;

create function app_is_staff() returns boolean
  language sql stable
  as $$ select coalesce(app_role() in ('associate', 'supervisor'), false) $$;

-- Looks up what a signed-in user is allowed to be at one merchant. The API calls this
-- before any claims exist, so it runs with the owner's rights and returns only the caller's
-- own relationship. Role and shopper identity always come from here, never from a token.
create function auth_context(p_user uuid, p_merchant uuid)
  returns table (staff_role text, shopper_id uuid, merchant_timezone text, merchant_slug text)
  language sql stable security definer
  set search_path = public, pg_temp
  as $$
    select
      (select ms.role::text from memberships ms
        where ms.user_id = p_user and ms.merchant_id = m.id),
      (select s.id from shoppers s
        where s.user_id = p_user and s.merchant_id = m.id),
      m.timezone,
      m.slug
    from merchants m
    where m.id = p_merchant
  $$;

revoke all on function auth_context(uuid, uuid) from public;
grant execute on function auth_context(uuid, uuid) to rd_app, rd_service;

alter table merchants enable row level security;
alter table app_users enable row level security;
alter table memberships enable row level security;
alter table shoppers enable row level security;

grant select on merchants, app_users, memberships, shoppers to rd_app;

create policy merchants_select on merchants for select to rd_app
  using (id = app_merchant());

create policy app_users_select on app_users for select to rd_app
  using (id = app_user());

create policy memberships_select on memberships for select to rd_app
  using (
    merchant_id = app_merchant()
    and (user_id = app_user() or app_role() = 'supervisor')
  );

create policy shoppers_select on shoppers for select to rd_app
  using (
    merchant_id = app_merchant()
    and (app_is_staff() or id = app_shopper())
  );
