-- Sign-in through an identity provider (Supabase Auth).
--
-- A provider's token proves who the user is and nothing else. Which merchant they are
-- working in, and on which side of the desk, comes with the request and is checked against
-- the database, exactly as before: a user gets a role or a shopper identity only where a
-- row says so. These two functions run before any claims exist, so they run with the
-- owner's rights and return only what the named user is entitled to know.

-- A merchant's ID from its public name in URLs.
create function auth_merchant_id(p_slug text) returns uuid
  language sql stable security definer
  set search_path = public, pg_temp
  as $$ select id from merchants where slug = p_slug $$;

-- Every place one user can sign in to: as staff, as a shopper, or both.
create function auth_relationships(p_user uuid)
  returns table (merchant_slug text, merchant_name text, role text)
  language sql stable security definer
  set search_path = public, pg_temp
  as $$
    select m.slug, m.name, ms.role::text
    from memberships ms join merchants m on m.id = ms.merchant_id
    where ms.user_id = p_user
    union all
    select m.slug, m.name, 'shopper'
    from shoppers s join merchants m on m.id = s.merchant_id
    where s.user_id = p_user
    order by 1, 3
  $$;

revoke all on function auth_merchant_id(text) from public;
revoke all on function auth_relationships(uuid) from public;
grant execute on function auth_merchant_id(text) to rd_app, rd_service;
grant execute on function auth_relationships(uuid) to rd_app, rd_service;
