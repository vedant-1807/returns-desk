# Returns Desk screens

The screens of Returns Desk, as a Next.js app.

| Path | Screen | Who |
| --- | --- | --- |
| `/` | Sign-in | everyone |
| `/chat` | Shopper chat: orders, requests, the conversation, status, policy citation | shoppers |
| `/staff` | Staff queue: cases, send to review, the trace of a case, and decisions for supervisors | associates, supervisors |
| `/policies` | Policy versions, read-only | everyone, limited to what their role may read |

## Run

The API must be running first (see the README one folder up).

```bash
npm install
npm run dev        # http://localhost:3000
```

| Setting | Meaning | Default |
| --- | --- | --- |
| `NEXT_PUBLIC_API_URL` | Where the API is | `http://localhost:8000` |
| `NEXT_PUBLIC_SUPABASE_URL` | Turns on sign-in through Supabase Auth | empty: local demo sign-in |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | The Supabase project's public key | empty |

The API must list this app's address in `RD_CORS_ORIGINS`.

## Sign-in

- **Local.** With no Supabase address set, the first page lists the seeded people. Picking
  one asks the API's `/dev/session` for a token. That endpoint only exists when the API
  runs with `RD_ENVIRONMENT=local`.
- **Deployed.** With a Supabase address set, the first page asks for an email and
  password, gets a token from Supabase Auth, and then asks the API where that person can
  go (`/me/merchants`). The token only says who the person is. Every later request names
  the merchant and the side of the desk in two headers, and the API checks both against
  its own records.

The token is kept in session storage for the browser tab.

## Browser tests

```bash
npm run e2e
```

Playwright drives both screens in the Chrome installed on this machine. The tests start
their own API on its own database (`scripts/e2e_api.py`, port 8010, keyword stand-in as
the model) and their own build of this app (port 3010), so they need nothing but the
Postgres container. They cover: a refund at Northline, review and approval at Vale, an
exchange in chat, and an associate sending a case to review. Screenshots go to
`docs/walkthrough/screens/`.

## Notes

- All rules live in the API. These screens show what the API returns and send what the
  person typed or clicked.
- The staff queue polls every five seconds; the chat polls while a case is with a
  supervisor or the payment provider.
