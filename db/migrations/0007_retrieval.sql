-- Policy retrieval: embedded chunks of policy sections, filtered by row-level security
-- before any passage can reach a model.

create extension if not exists vector;

-- One or more chunks per policy section. Each chunk carries its merchant, policy version,
-- audience and source section, so every permission check happens here in the database.
-- The vector size is fixed at 768: nomic-embed-text locally, bge-base-en-v1.5 when hosted.
-- Vectors from different models are never comparable, so each row records its model and
-- every search filters on it.
create table policy_chunks (
  id uuid primary key default gen_random_uuid(),
  merchant_id uuid not null,
  policy_version_id uuid not null,
  section_id uuid not null references policy_sections (id),
  section_key text not null,
  title text not null,
  audience policy_audience not null,
  chunk_index integer not null default 0,
  content text not null,
  embedding vector(768) not null,
  embedding_model text not null,
  created_at timestamptz not null default now(),
  unique (section_id, chunk_index, embedding_model),
  foreign key (merchant_id, policy_version_id) references policy_versions (merchant_id, id)
);

create index policy_chunks_embedding on policy_chunks using hnsw (embedding vector_cosine_ops);
create index policy_chunks_version on policy_chunks (policy_version_id);

alter table policy_chunks enable row level security;

grant select on policy_chunks to rd_app;
grant select, insert, update, delete on policy_chunks to rd_service;

-- Same audience rule as policy_sections: a shopper reads customer text only, an associate
-- adds associate text, and only a supervisor reads supervisor text.
create policy policy_chunks_select on policy_chunks for select to rd_app
  using (
    merchant_id = app_merchant()
    and (
      audience = 'customer'
      or (audience = 'associate' and app_is_staff())
      or (audience = 'supervisor' and app_role() = 'supervisor')
    )
  );

-- A counter that moves whenever anyone's staff role at a merchant changes. Cached search
-- results are keyed on it, so a role change can never be answered from an older cache entry.
alter table merchants add column permission_version bigint not null default 1;

create function bump_permission_version() returns trigger
  language plpgsql security definer
  set search_path = public, pg_temp
  as $$
  begin
    if tg_op in ('UPDATE', 'DELETE') then
      update merchants set permission_version = permission_version + 1
      where id = old.merchant_id;
    end if;
    if tg_op = 'INSERT' or (tg_op = 'UPDATE' and new.merchant_id is distinct from old.merchant_id) then
      update merchants set permission_version = permission_version + 1
      where id = new.merchant_id;
    end if;
    return null;
  end $$;

create trigger memberships_permission_version
  after insert or update or delete on memberships
  for each row execute function bump_permission_version();

-- auth_context now also returns the merchant's permission version.
drop function auth_context(uuid, uuid);

create function auth_context(p_user uuid, p_merchant uuid)
  returns table (
    staff_role text,
    shopper_id uuid,
    merchant_timezone text,
    merchant_slug text,
    permission_version bigint
  )
  language sql stable security definer
  set search_path = public, pg_temp
  as $$
    select
      (select ms.role::text from memberships ms
        where ms.user_id = p_user and ms.merchant_id = m.id),
      (select s.id from shoppers s
        where s.user_id = p_user and s.merchant_id = m.id),
      m.timezone,
      m.slug,
      m.permission_version
    from merchants m
    where m.id = p_merchant
  $$;

revoke all on function auth_context(uuid, uuid) from public;
grant execute on function auth_context(uuid, uuid) to rd_app, rd_service;
