# Returns Desk screens

The two screens of Returns Desk, as a Next.js app.

| Path | Screen | Who |
| --- | --- | --- |
| `/` | Local sign-in: pick a seeded person | everyone |
| `/chat` | Shopper chat: orders, requests, the conversation, status and policy citation | shoppers |
| `/staff` | Staff queue: cases for both staff roles, plus pending decisions for supervisors | associates, supervisors |

## Run

The API must be running first (see the README one folder up).

```bash
npm install
npm run dev        # http://localhost:3000
```

The API address comes from `NEXT_PUBLIC_API_URL` and defaults to `http://localhost:8000`.
The API must list this app's origin in `RD_CORS_ORIGINS`.

## Notes

- Sign-in uses the API's `/dev/session`, which exists only when the API runs with
  `RD_ENVIRONMENT=local`. Supabase Auth replaces it in the deploy milestone.
- The token is kept in session storage for the browser tab and sent as a bearer token.
- All rules live in the API. These screens show what the API returns and send what the
  person typed or clicked.
