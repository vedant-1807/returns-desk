"use client";

import Link from "next/link";
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
  type Trace,
} from "@/lib/api";
import { useSession } from "./use-session";

const FILTERS = ["all", "open", "awaiting_customer", "awaiting_supervisor", "resolved", "declined"];
// A case can be sent to a supervisor while it is still with the shopper or the agent.
const CAN_SEND_TO_REVIEW = new Set(["open", "awaiting_customer"]);

export default function StaffQueue() {
  const { session, signOut } = useSession(["associate", "supervisor"]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<CaseView | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [reply, setReply] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const isSupervisor = session?.role === "supervisor";

  const refresh = useCallback(async () => {
    if (!session) return;
    const caseData = await api<{ cases: CaseSummary[] }>("/cases", { session });
    setCases(caseData.cases);
    if (session.role === "supervisor") {
      const reviewData = await api<{ reviews: Review[] }>("/reviews", { session });
      setReviews(reviewData.reviews);
    }
  }, [session]);

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
    if (!session) return;
    setError(null);
    setTrace(null);
    setReviewNote("");
    const data = await api<{ case: CaseView }>(`/cases/${caseId}`, { session });
    setSelected(data.case);
  }

  async function decide(review: Review, decision: "approve" | "decline") {
    if (!session || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const data = await api<{
        decision: { decision: string; already_decided: boolean };
        case: CaseView;
      }>(`/reviews/${review.review_id}/decision`, {
        method: "POST",
        session,
        body: { decision, note: notes[review.review_id] ?? "" },
      });
      setNotice(
        data.decision.already_decided
          ? `This was already ${data.decision.decision} by someone else. Nothing changed.`
          : `Recorded: ${data.decision.decision}.`,
      );
      setSelected(data.case);
      setTrace(null);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        // The proposal changed, a hard rule now blocks it, or the replacement sold out.
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
    if (!session || !selected || !reply.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const data = await api<{ case: CaseView }>(`/cases/${selected.id}/messages`, {
        method: "POST",
        session,
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

  async function sendToReview() {
    if (!session || !selected || !reviewNote.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const data = await api<{ case: CaseView; already_requested: boolean }>(
        `/cases/${selected.id}/review-request`,
        { method: "POST", session, body: { note: reviewNote } },
      );
      setSelected(data.case);
      setReviewNote("");
      setNotice(
        data.already_requested
          ? "This case was already sent to review."
          : "Sent to review. A supervisor decides once the shopper confirms.",
      );
      refresh().catch(() => undefined);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not send to review");
    } finally {
      setBusy(false);
    }
  }

  async function loadTrace() {
    if (!session || !selected) return;
    setError(null);
    try {
      const data = await api<{ trace: Trace }>(`/cases/${selected.id}/trace`, { session });
      setTrace(data.trace);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load the trace");
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
        <div className="row gap">
          <Link className="link" href="/policies">
            Policies
          </Link>
          <button className="link" onClick={signOut}>
            Sign out
          </button>
        </div>
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
                <div key={review.review_id} className="card" data-testid="review-card">
                  <div className="row">
                    <h3>
                      {review.proposed_action === "exchange"
                        ? `Exchange · ${itemLabel(review.item)} for ${review.replacement_variant}`
                        : `Refund ${money(review.amount_cents)} · ${itemLabel(review.item)}`}
                    </h3>
                    <button className="link" onClick={() => open(review.case_id)}>
                      Open case
                    </button>
                  </div>
                  <dl className="facts">
                    <dt>Order</dt>
                    <dd>{review.item.order_number}</dd>
                    {review.proposed_action === "exchange" && (
                      <>
                        <dt>Value of the item</dt>
                        <dd>{money(review.amount_cents)} (no money moves)</dd>
                      </>
                    )}
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
                    aria-label="Decision note"
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
                {item.requested_action === "exchange" && item.replacement_variant && (
                  <span className="muted"> · exchange for {item.replacement_variant}</span>
                )}
                {item.needs_staff && CAN_SEND_TO_REVIEW.has(item.state) && (
                  <span className="flag">needs a person</span>
                )}
                {item.review_requested && <span className="flag">review requested</span>}
              </span>
              <span className={`pill state-${item.state}`}>{STATE_LABEL[item.state]}</span>
            </button>
          ))}
        </section>

        <aside className="side wide">
          {selected ? (
            <div className="card" data-testid="case-panel">
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
                {selected.exchange?.replacement_variant && (
                  <>
                    <dt>Exchange for</dt>
                    <dd>
                      {selected.exchange.replacement_variant}
                      {selected.exchange.fulfillment && (
                        <>
                          {" "}
                          · <code>{selected.exchange.fulfillment.reference}</code>
                        </>
                      )}
                    </dd>
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
                {selected.review_requested && (
                  <>
                    <dt>Review</dt>
                    <dd>
                      Requested {new Date(selected.review_requested.at).toLocaleString()}
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
                    className={`bubble ${message.author_kind === "shopper" ? "theirs" : "mine"} ${
                      message.audience === "internal" ? "internal" : ""
                    }`}
                  >
                    <span className="author">
                      {message.author_kind}
                      {message.audience === "internal" && " · internal note"}
                    </span>
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

              {CAN_SEND_TO_REVIEW.has(selected.state) && !selected.review_requested && (
                <form
                  className="composer"
                  onSubmit={(event) => {
                    event.preventDefault();
                    sendToReview();
                  }}
                >
                  <input
                    value={reviewNote}
                    onChange={(event) => setReviewNote(event.target.value)}
                    placeholder="Why a supervisor should look (internal)"
                    aria-label="Reason for review"
                    maxLength={2000}
                  />
                  <button type="submit" className="secondary" disabled={busy || !reviewNote.trim()}>
                    Send to review
                  </button>
                </form>
              )}

              <div className="row gap">
                <button className="link" onClick={loadTrace}>
                  {trace ? "Refresh trace" : "Show trace"}
                </button>
                {selected.trace?.langfuse_url && (
                  <a className="link" href={selected.trace.langfuse_url} target="_blank" rel="noreferrer">
                    Open in Langfuse
                  </a>
                )}
              </div>
              {trace && <TraceView trace={trace} />}
            </div>
          ) : (
            <p className="muted">Select a case to see the conversation.</p>
          )}
        </aside>
      </div>
    </main>
  );
}

function TraceView({ trace }: { trace: Trace }) {
  const waits = Object.entries(trace.totals.human_wait_seconds);
  return (
    <div className="trace" data-testid="trace">
      <p className="small muted">
        Service time {trace.totals.active_ms} ms · {trace.totals.model_attempts} model
        attempt{trace.totals.model_attempts === 1 ? "" : "s"}
        {trace.totals.model_attempts_not_ok > 0 &&
          ` (${trace.totals.model_attempts_not_ok} not ok)`}{" "}
        · {trace.totals.input_tokens} tokens in, {trace.totals.output_tokens} out
        {waits.map(([who, seconds]) => ` · waited ${Math.round(seconds)} s for ${who}`)}
        {trace.totals.unpriced_attempts > 0 &&
          ` · ${trace.totals.unpriced_attempts} unpriced attempts`}
      </p>
      <ol className="spans">
        {trace.spans.map((span) => (
          <li
            key={span.span_id}
            className={`span kind-${span.kind} ${span.parent_id ? "child" : ""} ${
              span.level !== "DEFAULT" ? "warn" : ""
            }`}
          >
            <span className="span-kind">{span.kind}</span>
            <span className="span-name">{span.name}</span>
            <span className="muted small">
              {span.kind === "generation" &&
                `${String(span.attributes.route)}/${String(span.attributes.model)} · ${String(
                  span.attributes.outcome,
                )} · `}
              {span.kind === "wait"
                ? `${Math.round(Number(span.attributes.seconds))} s`
                : `${span.ms} ms`}
            </span>
          </li>
        ))}
      </ol>
      <p className="small muted">
        Request IDs: {trace.request_ids.map((id) => id.slice(0, 16)).join(", ")}
      </p>
    </div>
  );
}
