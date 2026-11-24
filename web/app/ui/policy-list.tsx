"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api, type Policy } from "@/lib/api";
import { useSession } from "./use-session";

const TYPE_LABEL: Record<string, string> = {
  customer_terms: "Customer terms",
  operational: "Internal approval rules",
};

/**
 * A read-only list of policy versions. The API returns only what the signed-in person may
 * read: a shopper sees customer terms, staff also see the internal rules their role allows.
 * Versions are published through the API, not from this page.
 */
export default function PolicyList() {
  const { session, signOut } = useSession(["shopper", "associate", "supervisor"]);
  // The list, and the moment it was loaded: "in force" is judged against that moment.
  const [loaded, setLoaded] = useState<{ policies: Policy[]; at: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!session) return;
    const load = window.setTimeout(() => {
      api<{ policies: Policy[] }>("/policies", { session })
        .then((data) => setLoaded({ policies: data.policies, at: Date.now() }))
        .catch((cause) => setError(String(cause.message ?? cause)));
    }, 0);
    return () => window.clearTimeout(load);
  }, [session]);

  if (!session) return <main className="page" />;
  const policies = loaded?.policies ?? null;
  const now = loaded?.at ?? 0;

  return (
    <main className="page narrow">
      <header className="topbar">
        <div>
          <strong>{session.merchantName}</strong>
          <span className="muted"> · policies</span>
        </div>
        <div className="row gap">
          <Link className="link" href={session.role === "shopper" ? "/chat" : "/staff"}>
            Back
          </Link>
          <button className="link" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>
      {error && <p className="error">{error}</p>}
      {!policies && !error && <p className="muted">Loading policies…</p>}
      {policies?.map((policy) => (
        <section key={policy.id} className="card">
          <h2>
            {policy.name}{" "}
            <span className="muted small">{TYPE_LABEL[policy.policy_type] ?? policy.policy_type}</span>
          </h2>
          {policy.versions.map((version) => {
            const from = new Date(version.effective_from);
            const to = version.effective_to ? new Date(version.effective_to) : null;
            const current = from.getTime() <= now && (!to || now < to.getTime());
            return (
              <details key={version.id} open={current}>
                <summary>
                  <strong>{version.version_label}</strong>{" "}
                  <span className="muted small">
                    from {from.toLocaleDateString()}
                    {to ? ` to ${to.toLocaleDateString()}` : ""}
                  </span>{" "}
                  {current && <span className="pill state-resolved">in force</span>}
                  {from.getTime() > now && <span className="pill state-processing">future</span>}
                </summary>
                {version.sections.map((section) => (
                  <blockquote key={section.section_key}>
                    <strong>{section.title}</strong>{" "}
                    <span className="muted small">for {section.audience}</span>
                    <br />
                    {section.body}
                  </blockquote>
                ))}
              </details>
            );
          })}
        </section>
      ))}
    </main>
  );
}
