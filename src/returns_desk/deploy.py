"""Helpers for the free-tier deployment (Supabase, Render, Vercel).

Two things a deployment needs that a local run does not:

* The seeded demo people must exist in Supabase Auth, under the same user IDs the seed
  gives them, so a Supabase sign-in maps straight onto the seeded shopper or staff member.
* Supabase's scheduler (pg_cron with pg_net) must call the API's reconcile endpoint every
  minute, because Render's free plan has no background workers.
"""

from __future__ import annotations

from typing import Any

import httpx2

from returns_desk.seeds.demo import USERS, Ids


def create_supabase_users(
    supabase_url: str,
    service_role_key: str,
    password: str,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Create (or reset the password of) every seeded person in Supabase Auth.

    Uses the Auth admin API, which needs the project's service-role key. Each user gets
    the ID the seed uses for them and a confirmed email, so they can sign in at once with
    the shared demo password. Safe to run again.
    """
    if len(password) < 12:
        raise ValueError("Use a demo password of at least 12 characters")
    results = []
    with httpx2.Client(
        base_url=supabase_url.rstrip("/"),
        headers={"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"},
        timeout=20.0,
        transport=transport,
    ) as client:
        for key, (email, name) in USERS.items():
            user_id = str(Ids.user(key))
            created = client.post(
                "/auth/v1/admin/users",
                json={
                    "id": user_id,
                    "email": email,
                    "password": password,
                    "email_confirm": True,
                    "user_metadata": {"display_name": name},
                },
            )
            if created.status_code < 300:
                returned = created.json().get("id")
                if returned != user_id:
                    raise RuntimeError(
                        f"Supabase created {email} as {returned}, not the seeded ID {user_id}. "
                        "Sign-in would not map to the seeded user; delete that auth user "
                        "and check that the project accepts custom user IDs."
                    )
                results.append({"user": key, "email": email, "result": "created"})
                continue
            # Already there from an earlier run: make sure the password is the demo one.
            updated = client.put(
                f"/auth/v1/admin/users/{user_id}",
                json={"password": password, "email_confirm": True},
            )
            if updated.status_code >= 300:
                raise RuntimeError(
                    f"Supabase refused {email}: create said {created.status_code} "
                    f"{created.text[:200]}, update said {updated.status_code} {updated.text[:200]}"
                )
            results.append({"user": key, "email": email, "result": "updated"})
    return results


def reconcile_cron_sql(api_url: str, internal_token: str) -> str:
    """SQL for the Supabase SQL editor: call POST /internal/reconcile once a minute.

    It contains the internal token, so treat the output as a secret. Running it again
    replaces the job.
    """
    if "'" in api_url or "'" in internal_token:
        raise ValueError("The URL and the token must not contain a single quote")
    url = api_url.rstrip("/") + "/internal/reconcile"
    return f"""\
create extension if not exists pg_cron;
create extension if not exists pg_net;

select cron.unschedule(jobid) from cron.job where jobname = 'returns-desk-reconcile';

select cron.schedule(
  'returns-desk-reconcile',
  '* * * * *',
  $job$
    select net.http_post(
      url := '{url}',
      headers := jsonb_build_object(
        'Authorization', 'Bearer {internal_token}',
        'Content-Type', 'application/json'
      ),
      body := '{{}}'::jsonb,
      timeout_milliseconds := 20000
    );
  $job$
);
"""
