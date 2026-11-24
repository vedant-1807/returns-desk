# Deploy Returns Desk for $0

This puts the whole system online on free plans. Nothing here needs a credit card.

| Piece | Service | Free plan |
| --- | --- | --- |
| Database, sign-in, scheduler | Supabase | Free |
| API, agent, tools, model gateway | Render | Free web service |
| The two screens | Vercel | Hobby |
| Model | Groq | Free plan (you have the key) |
| Tests and evals | GitHub Actions | Free |
| Traces (optional) | Langfuse Cloud | Hobby |
| Payments (optional) | Stripe | Test mode |

## What has been checked, and what has not

Read this first. It says how much of this guide is proven.

**Checked on this machine**

- The Docker image builds, starts as a non-root user, runs the migrations and serves real
  requests against Postgres.
- The API accepts tokens shaped like Supabase's, signed both ways Supabase signs them
  (shared secret and published key).
- The two screens build with Supabase sign-in turned on, and pass browser tests with the
  local sign-in.
- The commands in steps 5 and 6 are tested against stand-ins for Supabase.

**Not checked**

- No real Supabase, Render or Vercel project was created. There were no accounts or
  logins for them on this machine.
- So the migrations have not run on Supabase's own Postgres. One line may need care:
  migration `0001` creates the role `rd_service` with `BYPASSRLS`. If Supabase refuses
  that, the first deploy fails at the migrate step with a clear error. Tell me and I will
  adjust it.
- Supabase's connection pooler has not been tried with the five database roles.

## Before you start

You need the code on GitHub. Render and Vercel both deploy from a GitHub repository.

```bash
cd ~/Projects/returns-desk
gh repo create returns-desk --private --source . --push
```

A private repository works. Make it public later if you want unlimited Actions minutes.

## Step 1: Supabase

1. Create a project at supabase.com. Pick a region near you. Save the database password.
2. Open **Project Settings**, then **API**. Copy the project URL, the `anon` key and the
   `service_role` key. Under **JWT Settings**, copy the JWT secret.
3. Open **Project Settings**, then **Database**, then **Connection string**. Choose
   **Session pooler**. It looks like this:

   ```
   postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```

   Use the session pooler, not the direct connection. Render's free plan cannot reach the
   direct one.
4. Make up four long random passwords, one per role. Then write five connection strings.
   Only the user name and password change:

   | Setting | User | Password |
   | --- | --- | --- |
   | `RD_ADMIN_DSN` | `postgres.<ref>` | the database password |
   | `RD_APP_DSN` | `rd_app.<ref>` | random password 1 |
   | `RD_SERVICE_DSN` | `rd_service.<ref>` | random password 2 |
   | `RD_PROCESSOR_DSN` | `rd_processor.<ref>` | random password 3 |
   | `RD_WORKFLOW_DSN` | `rd_workflow.<ref>` | random password 4 |

   The roles do not exist yet. The first deploy creates them and gives them these
   passwords.

## Step 2: Render

1. At render.com choose **New**, then **Blueprint**. Pick the repository. Render reads
   `render.yaml`.
2. Fill in the values it asks for:

   | Setting | Value |
   | --- | --- |
   | `RD_ADMIN_DSN` and the four others | the five strings from step 1 |
   | `RD_JWT_SECRET` | the Supabase JWT secret |
   | `RD_JWT_JWKS_URL` | leave empty, unless your project signs with asymmetric keys. Then use `https://<ref>.supabase.co/auth/v1/.well-known/jwks.json` |
   | `RD_GROQ_API_KEY` | your Groq key |
   | `RD_CORS_ORIGINS` | leave empty for now. You set it in step 5 |
   | `RD_LANGFUSE_PUBLIC_KEY`, `RD_LANGFUSE_SECRET_KEY` | leave empty, or see "Optional" below |

3. Deploy. The first start runs the database migrations.
4. Check it: open `https://<your-service>.onrender.com/healthz?deep=1`. You should see
   `{"status":"ok","database":"ok"}`.
5. In Render, open the service's **Environment** page and copy the value of
   `RD_INTERNAL_TOKEN`. You need it in step 4.

## Step 3: demo data and demo people

Run these on your Mac. They talk to Supabase directly.

```bash
cd ~/Projects/returns-desk

# The two merchants, their policies, orders and stock.
RD_ADMIN_DSN='<admin string>' RD_PROCESSOR_DSN='<processor string>' RD_EMBEDDER=hash \
  uv run returns-desk seed --fixture two_jackets

# Sign-in accounts for the seven demo people, all with one password you choose.
SUPABASE_SERVICE_ROLE_KEY='<service_role key>' RD_DEMO_PASSWORD='<at least 12 characters>' \
  uv run returns-desk supabase-users --url https://<ref>.supabase.co
```

The people are `maya@example.test` and `omar@example.test` (shoppers at both stores),
`nina.associate@northline.test`, `sam.supervisor@northline.test`,
`lee.supervisor@northline.test`, `alex.associate@vale.test` and
`vera.supervisor@vale.test`.

## Step 4: the scheduler

Render's free plan has no background worker. Supabase calls the API once a minute instead.

```bash
RD_INTERNAL_TOKEN='<value from Render>' \
  uv run returns-desk cron-sql --api-url https://<your-service>.onrender.com
```

Copy what it prints into the Supabase **SQL Editor** and run it. The output contains the
internal token, so do not share it.

## Step 5: Vercel

1. At vercel.com choose **Add New**, then **Project**. Pick the repository.
2. Set **Root Directory** to `web`.
3. Add three environment variables:

   | Name | Value |
   | --- | --- |
   | `NEXT_PUBLIC_API_URL` | `https://<your-service>.onrender.com` |
   | `NEXT_PUBLIC_SUPABASE_URL` | `https://<ref>.supabase.co` |
   | `NEXT_PUBLIC_SUPABASE_ANON_KEY` | the Supabase `anon` key |

4. Deploy. Copy the address Vercel gives you.
5. Back in Render, set `RD_CORS_ORIGINS` to that address (no slash at the end). Render
   restarts the service.

Open the Vercel address and sign in as `maya@example.test` with your demo password.

## Step 6: keep it awake

Free services sleep. Two things keep this one awake:

- The scheduler from step 4 calls the API every minute, so Render does not spin it down.
- A GitHub workflow pings the API once a day as a backup, so Supabase sees activity.

In the GitHub repository open **Settings**, **Secrets and variables**, **Actions**:

- Under **Variables**, add `API_URL` with your Render address. Without it the daily ping
  is skipped.
- Under **Secrets**, add `GROQ_API_KEY` if you want the nightly run to test the real model.

## Optional

**Langfuse traces.** Create a free project at cloud.langfuse.com. Put its two keys in
`RD_LANGFUSE_PUBLIC_KEY` and `RD_LANGFUSE_SECRET_KEY` on Render. Staff then see an
"Open in Langfuse" link on each case. The export code is tested against a stand-in, not
against Langfuse itself.

**Stripe test mode.** On Render set `RD_PAYMENT_PROVIDER=stripe` and
`RD_STRIPE_SECRET_KEY` to a test key (`sk_test_...`). Live keys are refused. In Stripe,
add a webhook endpoint for `https://<your-service>.onrender.com/webhooks/payments` with
the `refund.created`, `refund.updated` and `refund.failed` events, and set
`RD_PAYMENT_WEBHOOK_SECRET` to its signing secret. Seed again afterwards: the seed makes
the test payments in Stripe.

## If something goes wrong

| What you see | Likely cause |
| --- | --- |
| The deploy fails at "migrate" | A database string is wrong, or Supabase refused a role. The log names the line |
| Sign-in works, then "You do not have access to this merchant" | Step 3 was skipped, so the demo people are not in the database |
| The screens load but every call fails | `RD_CORS_ORIGINS` on Render does not match the Vercel address exactly |
| The first request takes about a minute | Render was asleep. Step 6 prevents this |
| A refund stays "pending verification" | The scheduler from step 4 is not running |
