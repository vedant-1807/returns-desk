-- Versioned policies.
--   customer_terms  what the shopper agreed to; chosen by purchase timestamp
--   operational     internal rules (approval limit, return-history rule); chosen at decision time
-- Versions are immutable. Publishing a revision closes the open version and adds a new one.

create type policy_type as enum ('customer_terms', 'operational');
create type policy_audience as enum ('customer', 'associate', 'supervisor');

create table policies (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null references merchants (id),
  policy_type policy_type not null,
  scope text not null default 'returns',
  name text not null,
  created_at timestamptz not null default now(),
  unique (merchant_id, id),
  unique (merchant_id, policy_type, scope)
);

create table policy_versions (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null,
  policy_id uuid not null,
  version_label text not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  rules jsonb not null,
  published_by uuid references app_users (id),
  published_at timestamptz not null default now(),
  unique (merchant_id, id),
  unique (policy_id, version_label),
  foreign key (merchant_id, policy_id) references policies (merchant_id, id),
  check (effective_to is null or effective_to > effective_from),
  -- Effective intervals of one policy can never overlap.
  constraint policy_versions_no_overlap
    exclude using gist (
      policy_id with =,
      tstzrange(effective_from, effective_to, '[)') with &&
    )
);

create function policy_versions_guard() returns trigger
  language plpgsql
  as $$
  begin
    if tg_op = 'DELETE' then
      raise exception 'policy versions are immutable and cannot be deleted';
    end if;
    if new.id is distinct from old.id
      or new.merchant_id is distinct from old.merchant_id
      or new.policy_id is distinct from old.policy_id
      or new.version_label is distinct from old.version_label
      or new.effective_from is distinct from old.effective_from
      or new.rules is distinct from old.rules
    then
      raise exception 'policy versions are immutable; publish a new version instead';
    end if;
    if old.effective_to is not null and new.effective_to is distinct from old.effective_to then
      raise exception 'a closed policy version cannot be reopened or moved';
    end if;
    return new;
  end $$;

create trigger policy_versions_guard
  before update or delete on policy_versions
  for each row execute function policy_versions_guard();

-- Source sections. Each carries its audience so filtering happens in the database.
-- Milestone 2 adds chunk embeddings next to these rows.
create table policy_sections (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null,
  policy_version_id uuid not null,
  section_key text not null,
  title text not null,
  audience policy_audience not null,
  body text not null,
  unique (policy_version_id, section_key),
  foreign key (merchant_id, policy_version_id) references policy_versions (merchant_id, id)
);

-- The decision engine needs the active operational rules even when the request comes from a
-- shopper, who must never read them. This is the one narrow door: it returns the rules for
-- the caller's own merchant to server code. `matches` lets the caller detect zero or
-- overlapping versions and send the case to staff instead of guessing.
create function app_operational_rules(p_at timestamptz)
  returns table (version_id uuid, version_label text, rules jsonb, matches bigint)
  language sql stable security definer
  set search_path = public, pg_temp
  as $$
    select pv.id, pv.version_label, pv.rules, count(*) over ()
    from policy_versions pv
    join policies p on p.id = pv.policy_id
    where p.merchant_id = app_merchant()
      and p.policy_type = 'operational'
      and p.scope = 'returns'
      and pv.effective_from <= p_at
      and (pv.effective_to is null or p_at < pv.effective_to)
  $$;

revoke all on function app_operational_rules(timestamptz) from public;
grant execute on function app_operational_rules(timestamptz) to rd_app, rd_service;

alter table policies enable row level security;
alter table policy_versions enable row level security;
alter table policy_sections enable row level security;

grant select, insert on policies, policy_versions, policy_sections to rd_app;
grant update (effective_to) on policy_versions to rd_app;

-- Customer terms are readable across the merchant. Operational rules are supervisor-only.
create policy policies_select on policies for select to rd_app
  using (
    merchant_id = app_merchant()
    and (policy_type = 'customer_terms' or app_role() = 'supervisor')
  );

create policy policy_versions_select on policy_versions for select to rd_app
  using (
    merchant_id = app_merchant()
    and exists (select 1 from policies p where p.id = policy_versions.policy_id)
  );

create policy policy_sections_select on policy_sections for select to rd_app
  using (
    merchant_id = app_merchant()
    and (
      audience = 'customer'
      or (audience = 'associate' and app_is_staff())
      or (audience = 'supervisor' and app_role() = 'supervisor')
    )
  );

-- Only supervisors publish, and only inside their own merchant.
create policy policy_versions_insert on policy_versions for insert to rd_app
  with check (merchant_id = app_merchant() and app_role() = 'supervisor');

create policy policy_versions_update on policy_versions for update to rd_app
  using (merchant_id = app_merchant() and app_role() = 'supervisor')
  with check (merchant_id = app_merchant());

create policy policy_sections_insert on policy_sections for insert to rd_app
  with check (merchant_id = app_merchant() and app_role() = 'supervisor');
