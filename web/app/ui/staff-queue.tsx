"use client";

import { useCallback, useEffect, useState } from "react";

import {
  api,
  ApiError,
  itemLabel,
  money,
  REASON_LABEL,
  requestId,
  STATE_LABEL,
  type CaseSummary,
  type CaseView,
  type Review,
} from "@/lib/api";
import { useSession } from "./use-session";

const FILTERS = ["all", "open", "awaiting_customer", "awaiting_supervisor", "resolved", "declined"];

export default function StaffQueue() {
  const { session, signOut } = useSession(["associate", "supervisor"]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<CaseView | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const token = session?.token;
  const isSupervisor = session?.role === "supervisor";

  const refresh = useCallback(async () => {
    if (!token) return;
    const caseData = await api<{ cases: CaseSummary[] }>("/cases", { token });
    setCases(caseData.cases);
    if (isSupervisor) {
      const reviewData = await api<{ reviews: Review[] }>("/reviews", { token });
      setReviews(reviewData.reviews);
    }
  }, [token, isSupervisor]);

  // Load once, then keep the queue fresh. State is only ever set from these callbacks.
  useEffect(() => {
    const load = () => {
      refresh().catch((cause) => setError(String(cause.message ?? cause)));
    };
    const first = window.setTimeout(load, 0);
    const timer = window.setInterval(load, 5000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [refresh]);

  async function open(caseId: string) {
    if (!token) return;
    setError(null);
    const data = await api<{ case: CaseView }>(`/cases/${caseId}`, { token });
    setSelected(data.case);
  }

  async function decide(review: Review, decision: "approve" | "decline") {
    if (!token || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const data = await api<{
        decision: { decision: string; already_decided: boolean };
        case: CaseView;
      }>(`/reviews/${review.review_id}/decision`, {
        method: "POST",
        token,
        body: { decision, note: notes[review.review_id] ?? "" },
      });
      setNotice(
        data.decision.already_decided
          ? `This was already ${data.decision.decision} by someone else. Nothing changed.`
          : `Recorded: ${data.decision.decision}.`,
      );
      setSelected(data.case);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        // The proposal changed, or a hard rule now blocks it. Show the current state.
        setNotice(`${cause.message} The queue has been refreshed.`);
      } else {
        setError(cause instanceof Error ? cause.message : "Could not record the decision");
      }
    } finally {
      setBusy(false);
      refresh().catch(() => undefined);
    }
  }

  async function sendReply() {
    if (!token || !selected || !reply.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const data = await api<{ case: CaseView }>(`/cases/${selected.id}/messages`, {
        method: "POST",
        token,
        body: { client_request_id: requestId(), text: reply },
      });
      setSelected(data.case);
      setReply("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not send");
    } finally {
      setBusy(false);
    }
  }

  if (!session) return <main className="page" />;
  const visible = cases.filter((item) => filter === "all" || item.state === filter);

  return (
    <main className="page">
      <header className="topbar">
        <div>
          <strong>{session.merchantName}</strong>
          <span className="muted">
            {" "}
            · staff queue · {session.name} ({session.role})
          </span>
        </div>
        <button className="link" onClick={signOut}>
          Sign out
        </button>
      </header>

      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error}</p>}

      <div className="columns">
        <section className="main">
          {isSupervisor && (
            <>
              <h2>Needs a decision ({reviews.length})</h2>
              {reviews.length === 0 && <p className="muted">Nothing is waiting for you.</p>}
              {reviews.map((review) => (
                <div key={review.review_id} className="card">
                  <div className="row">
                    <h3>
                      {review.proposed_action === "refund" ? "Refund" : review.proposed_action}{" "}
                      {money(review.amount_cents)} · {itemLabel(review.item)}
                    </h3>
                    <button className="link" onClick={() => open(review.case_id)}>
                      Open case
                    </button>
                  </div>
                  <dl className="facts">
                    <dt>Order</dt>
                    <dd>{review.item.order_number}</dd>
                    <dt>Shopper&rsquo;s reason</dt>
                    <dd>{review.request_reason.replace(/_/g, " ")}</dd>
                    <dt>Why it needs you</dt>
                    <dd>
                      {review.reasons.map((reason) => REASON_LABEL[reason] ?? reason).join("; ")}
                    </dd>
                    <dt>Internal rule</dt>
                    <dd>
                      {review.internal_rule ? (
                        <>
                          {review.internal_rule.version_label}
                          {review.internal_rule.sections.map((section) => (
                            <div key={section.section_key} className="muted small">
                              {section.title}: {section.body}
                            </div>
                          ))}
                        </>
                      ) : (
                        "None found"
                      )}
                    </dd>
                    <dt>Evidence</dt>
                    <dd className="small">
                      {Object.entries(review.internal_evidence)
                        .map(([key, value]) => `${key.replace(/_/g, " ")}: ${String(value)}`)
                        .join(" · ")}
                    </dd>
                    <dt>Customer policy</dt>
                    <dd>
                      {review.applicable_policy.map((citation) => (
                        <div key={citation.section_key} className="small">
                          <strong>
                            {citation.title} ({citation.version_label}):
                          </strong>{" "}
                          {citation.body}
                        </div>
                      ))}
                    </dd>
                  </dl>
                  <textarea
                    placeholder="Note (kept internal)"
                    value={notes[review.review_id] ?? ""}
                    onChange={(event) =>
                      setNotes({ ...notes, [review.review_id]: event.target.value })
                    }
                    maxLength={2000}
                  />
                  <div className="row gap">
                    <button onClick={() => decide(review, "approve")} disabled={busy}>
                      Approve
                    </button>
                    <button
                      className="secondary"
                      onClick={() => decide(review, "decline")}
                      disabled={busy}
                    >
                      Decline
                    </button>
                  </div>
                </div>
              ))}
            </>
          )}

          <h2>Cases</h2>
          <div className="row gap wrap">
            {FILTERS.map((name) => (
              <button
                key={name}
                className={filter === name ? "chip active" : "chip"}
                onClick={() => setFilter(name)}
              >
                {name === "all" ? "All" : STATE_LABEL[name]}
              </button>
            ))}
          </div>
          {visible.length === 0 && <p className="muted">No cases here.</p>}
          {visible.map((item) => (
            <button
              key={item.id}
              className={`listitem ${selected?.id === item.id ? "active" : ""}`}
              onClick={() => open(item.id)}
            >
              <span>
                {item.description ? itemLabel(item) : "Question"}
                {item.order_number && <span className="muted"> · {item.order_number}</span>}
              </span>
              <span className={`pill state-${item.state}`}>{STATE_LABEL[item.state]}</span>
            </button>
          ))}
        </section>

        <aside className="side wide">
          {selected ? (
            <div className="card">
              <div className="row">
                <h2>{itemLabel(selected.item)}</h2>
                <span className={`pill state-${selected.state}`}>
                  {STATE_LABEL[selected.state]}
                </span>
              </div>
              <dl className="facts">
                <dt>Case</dt>
                <dd>
                  <code>{selected.id}</code>
                </dd>
                {selected.amount_cents !== null && (
                  <>
                    <dt>Amount</dt>
                    <dd>{money(selected.amount_cents)}</dd>
                  </>
                )}
                {selected.reason_code && (
                  <>
                    <dt>Reason</dt>
                    <dd>{selected.reason_code.replace(/_/g, " ")}</dd>
                  </>
                )}
                {selected.receipt && (
                  <>
                    <dt>Refund</dt>
                    <dd>
                      <code>{selected.receipt.refund_reference}</code>
                    </dd>
                  </>
                )}
                {selected.decision && (
                  <>
                    <dt>Decision</dt>
                    <dd>
                      {selected.decision.decision} on proposal v
                      {selected.decision.proposal_version},{" "}
                      {new Date(selected.decision.decided_at).toLocaleString()}
                      {selected.decision.note && (
                        <div className="muted small">Note: {selected.decision.note}</div>
                      )}
                    </dd>
                  </>
                )}
                {selected.review && (
                  <>
                    <dt>Review reason</dt>
                    <dd>
                      {selected.review.reasons
                        .map((reason) => REASON_LABEL[reason] ?? reason)
                        .join("; ")}
                    </dd>
                  </>
                )}
              </dl>
              <div className="messages inline">
                {selected.messages.map((message, index) => (
                  <div
                    key={index}
                    className={`bubble ${message.author_kind === "shopper" ? "theirs" : "mine"}`}
                  >
                    <span className="author">{message.author_kind}</span>
                    {message.body}
                  </div>
                ))}
              </div>
              <form
                className="composer"
                onSubmit={(event) => {
                  event.preventDefault();
                  sendReply();
                }}
              >
                <input
                  value={reply}
                  onChange={(event) => setReply(event.target.value)}
                  placeholder="Write to the shopper"
                  aria-label="Reply to the shopper"
                  maxLength={4000}
                />
                <button type="submit" disabled={busy || !reply.trim()}>
                  Send
                </button>
              </form>
            </div>
          ) : (
            <p className="muted">Select a case to see the conversation.</p>
          )}
        </aside>
      </div>
    </main>
  );
}
