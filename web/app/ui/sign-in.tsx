"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  api,
  API_URL,
  saveSession,
  SUPABASE_URL,
  supabaseSignIn,
  surfaceFor,
  type DevUser,
  type Membership,
  type Role,
} from "@/lib/api";

const ROLE_ORDER: Role[] = ["shopper", "associate", "supervisor"];

export default function SignIn() {
  // With Supabase configured, people sign in with an email and password. Without it the
  // app offers the local demo sign-in, which the API only serves in local development.
  return SUPABASE_URL ? <ProviderSignIn /> : <DemoSignIn />;
}

function ProviderSignIn() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState<string | null>(null);
  const [choices, setChoices] = useState<Membership[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function signIn() {
    setBusy(true);
    setError(null);
    try {
      const accessToken = await supabaseSignIn(email.trim(), password);
      // The token says who this is. Where they can go comes from the API's own records.
      const data = await api<{ merchants: Membership[] }>("/me/merchants", {
        token: accessToken,
      });
      if (data.merchants.length === 0) {
        setError("This account is not set up at any store yet.");
      } else {
        setToken(accessToken);
        setChoices(data.merchants);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  function choose(choice: Membership) {
    if (!token) return;
    saveSession({
      token,
      role: choice.role,
      merchant: choice.merchant_slug,
      surface: surfaceFor(choice.role),
      merchantName: choice.merchant_name,
      name: email.trim(),
      email: email.trim(),
    });
    router.push(choice.role === "shopper" ? "/chat" : "/staff");
  }

  return (
    <main className="page narrow">
      <h1>Returns Desk</h1>
      {error && <p className="error">{error}</p>}
      {choices.length === 0 ? (
        <form
          className="card stack"
          onSubmit={(event) => {
            event.preventDefault();
            signIn();
          }}
        >
          <h2>Sign in</h2>
          <label>
            Email
            <input
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          <button type="submit" disabled={busy || !email || !password}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      ) : (
        <section className="card">
          <h2>Where to?</h2>
          <ul className="people">
            {choices.map((choice) => (
              <li key={`${choice.merchant_slug}:${choice.role}`}>
                <button onClick={() => choose(choice)}>
                  <span className="person">{choice.merchant_name}</span>
                  <span className={`pill role-${choice.role}`}>{choice.role}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}

function DemoSignIn() {
  const router = useRouter();
  const [users, setUsers] = useState<DevUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    api<{ users: DevUser[] }>("/dev/users")
      .then((data) => setUsers(data.users))
      .catch(() =>
        setError(
          `Could not reach the API at ${API_URL}. Start it with ` +
            "`uv run uvicorn --factory returns_desk.api:create_app`.",
        ),
      );
  }, []);

  async function signIn(user: DevUser) {
    setBusy(`${user.email}:${user.merchant}`);
    try {
      const result = await api<{ token: string; role: Role; merchant: string }>("/dev/session", {
        method: "POST",
        body: { email: user.email, merchant: user.merchant },
      });
      saveSession({
        token: result.token,
        role: result.role,
        merchant: result.merchant,
        surface: surfaceFor(result.role),
        merchantName: user.merchant_name,
        name: user.display_name,
        email: user.email,
      });
      router.push(result.role === "shopper" ? "/chat" : "/staff");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in failed");
      setBusy(null);
    }
  }

  const merchants = Array.from(new Set((users ?? []).map((u) => u.merchant)));

  return (
    <main className="page narrow">
      <h1>Returns Desk</h1>
      <p className="muted">
        Local demo sign-in. Pick a seeded person. Shoppers open the chat, staff open the queue.
      </p>
      {error && <p className="error">{error}</p>}
      {!users && !error && <p className="muted">Loading people…</p>}
      {merchants.map((merchant) => {
        const people = (users ?? [])
          .filter((u) => u.merchant === merchant)
          .sort((a, b) => ROLE_ORDER.indexOf(a.role) - ROLE_ORDER.indexOf(b.role));
        return (
          <section key={merchant} className="card">
            <h2>{people[0]?.merchant_name}</h2>
            <ul className="people">
              {people.map((user) => {
                const key = `${user.email}:${user.merchant}`;
                return (
                  <li key={key}>
                    <button onClick={() => signIn(user)} disabled={busy !== null}>
                      <span className="person">{user.display_name}</span>
                      <span className={`pill role-${user.role}`}>{user.role}</span>
                      {busy === key && <span className="muted"> signing in…</span>}
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </main>
  );
}
