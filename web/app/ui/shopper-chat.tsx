"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  CUSTOMER_STATUS_LABEL,
  itemLabel,
  money,
  requestId,
  type CaseSummary,
  type CaseView,
  type Order,
} from "@/lib/api";
import { useSession } from "./use-session";

// While a case is in one of these states something is happening without the shopper, so
// the screen keeps checking.
const POLL_STATES = new Set(["awaiting_supervisor", "processing", "reconciling"]);

export default function ShopperChat() {
  const { session, signOut } = useSession(["shopper"]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [current, setCurrent] = useState<CaseView | null>(null);
  const [orderId, setOrderId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const refreshLists = useCallback(async () => {
    if (!session) return;
    const [orderData, caseData] = await Promise.all([
      api<{ orders: Order[] }>("/orders", { session }),
      api<{ cases: CaseSummary[] }>("/cases", { session }),
    ]);
    setOrders(orderData.orders);
    setCases(caseData.cases);
  }, [session]);

  // Load the lists once the session is known. State is only set from this callback.
  useEffect(() => {
    const first = window.setTimeout(() => {
      refreshLists().catch((cause) => setError(String(cause.message ?? cause)));
    }, 0);
    return () => window.clearTimeout(first);
  }, [refreshLists]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [current?.messages.length]);

  // Poll while the case is with a supervisor or the payment provider.
  useEffect(() => {
    if (!session || !current || !POLL_STATES.has(current.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const data = await api<{ case: CaseView }>(`/cases/${current.id}`, { session });
        setCurrent(data.case);
        if (!POLL_STATES.has(data.case.state)) refreshLists();
      } catch {
        // A failed poll is retried on the next tick.
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [session, current, refreshLists]);

  async function openCase(id: string) {
    if (!session) return;
    setError(null);
    const data = await api<{ case: CaseView }>(`/cases/${id}`, { session });
    setCurrent(data.case);
  }

  async function send(text: string) {
    if (!session || !text.trim() || sending) return;
    setSending(true);
    setError(null);
    try {
      const data = current
        ? await api<{ case: CaseView }>(`/cases/${current.id}/messages`, {
            method: "POST",
            session,
            body: { client_request_id: requestId(), text },
          })
        : await api<{ case: CaseView }>("/cases", {
            method: "POST",
            session,
            body: {
              client_request_id: requestId(),
              message: text,
              ...(orderId ? { order_id: orderId } : {}),
            },
          });
      setCurrent(data.case);
      setDraft("");
      refreshLists();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not send");
    } finally {
      setSending(false);
    }
  }

  function startNew(order: string | null) {
    setCurrent(null);
    setOrderId(order);
    setDraft("");
    setError(null);
  }

  if (!session) return <main className="page" />;
  const chosenOrder = orders.find((o) => o.id === orderId);

  return (
    <main className="page">
      <header className="topbar">
        <div>
          <strong>{session.merchantName}</strong>
          <span className="muted"> · returns help for {session.name}</span>
        </div>
        <button className="link" onClick={signOut}>
          Sign out
        </button>
      </header>

      <div className="columns">
        <aside className="side">
          <h2>Your orders</h2>
          {orders.length === 0 && <p className="muted">No orders found.</p>}
          {orders.map((order) => (
            <div key={order.id} className="card compact">
              <div className="row">
                <strong>{order.order_number}</strong>
                <button className="link" onClick={() => startNew(order.id)}>
                  Ask about this order
                </button>
              </div>
              <ul className="lines">
                {order.lines.map((line) => (
                  <li key={line.id}>
                    {itemLabel(line)}
                    <span className="muted">
                      {" "}
                      · {money(line.remaining_cents)} refundable
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}

          <h2>Your requests</h2>
          <button className="secondary" onClick={() => startNew(null)}>
            New request
          </button>
          {cases.map((item) => (
            <button
              key={item.id}
              className={`listitem ${current?.id === item.id ? "active" : ""}`}
              onClick={() => openCase(item.id)}
            >
              <span>{item.description ? itemLabel(item) : "Question"}</span>
              <span className={`pill state-${item.state}`}>{item.state.replace(/_/g, " ")}</span>
            </button>
          ))}
        </aside>

        <section className="main">
          {current ? (
            <CaseSummaryCard view={current} />
          ) : (
            <div className="card">
              <h2>How can we help?</h2>
              <p className="muted">
                Tell us what happened in your own words
                {chosenOrder ? ` about order ${chosenOrder.order_number}` : ""}. For example:
                &ldquo;The blue jacket arrived damaged. Please refund just that one.&rdquo; You
                can also ask to exchange an item for another size or colour.
              </p>
            </div>
          )}

          <div className="messages">
            {current?.messages.map((message, index) => (
              <div
                key={index}
                className={`bubble ${message.author_kind === "shopper" ? "mine" : "theirs"}`}
              >
                {message.author_kind !== "shopper" && message.author_kind !== "system" && (
                  <span className="author">{message.author_kind}</span>
                )}
                {message.body}
              </div>
            ))}
            <div ref={bottom} />
          </div>

          {error && <p className="error">{error}</p>}

          {current?.next_action === "confirm" && (
            <div className="row gap">
              <button onClick={() => send("Yes")} disabled={sending}>
                Yes, go ahead
              </button>
              <button className="secondary" onClick={() => send("No")} disabled={sending}>
                No
              </button>
            </div>
          )}

          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              send(draft);
            }}
          >
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={current ? "Type a reply" : "Describe the problem"}
              aria-label="Message"
              maxLength={4000}
            />
            <button type="submit" disabled={sending || !draft.trim()}>
              {sending ? "Sending…" : "Send"}
            </button>
          </form>
        </section>
      </div>
    </main>
  );
}

function CaseSummaryCard({ view }: { view: CaseView }) {
  return (
    <div className="card">
      <div className="row">
        <h2>{itemLabel(view.item)}</h2>
        <span className={`pill status-${view.status}`}>
          {CUSTOMER_STATUS_LABEL[view.status] ?? view.status}
        </span>
      </div>
      <p className="muted">{view.status_message}</p>
      <dl className="facts">
        {view.item.order_number && (
          <>
            <dt>Order</dt>
            <dd>{view.item.order_number}</dd>
          </>
        )}
        {view.item.quantity !== null && view.item.description && (
          <>
            <dt>Quantity</dt>
            <dd>{view.item.quantity}</dd>
          </>
        )}
        {view.amount_cents !== null && (
          <>
            <dt>Amount</dt>
            <dd>{money(view.amount_cents)}</dd>
          </>
        )}
        {view.exchange?.replacement_variant && (
          <>
            <dt>Exchange for</dt>
            <dd>{view.exchange.replacement_variant}</dd>
          </>
        )}
        {view.exchange?.fulfillment && (
          <>
            <dt>Exchange reference</dt>
            <dd>
              <code>{view.exchange.fulfillment.reference}</code>
            </dd>
          </>
        )}
        {view.receipt && (
          <>
            <dt>Refund reference</dt>
            <dd>
              <code>{view.receipt.refund_reference}</code>
            </dd>
          </>
        )}
        <dt>Request reference</dt>
        <dd>
          <code>{view.id}</code>
        </dd>
      </dl>
      {view.policy_citations.length > 0 && (
        <details>
          <summary>Policy this is based on</summary>
          {view.policy_citations.map((citation) => (
            <blockquote key={citation.section_key}>
              <strong>
                {citation.title} ({citation.version_label})
              </strong>
              <br />
              {citation.body}
            </blockquote>
          ))}
        </details>
      )}
    </div>
  );
}
