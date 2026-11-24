"""Traces: one per case, built from the audit trail.

What these tests hold the trace to:

* one correlation ID from the shopper's request to the payment processor's own record,
* every model attempt, tool call, state change and processor call is there,
* IDs and outcome codes only: no message text and no policy text,
* time spent waiting for a person is reported apart from service time,
* a reader sees what their role may see today, old traces included.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import httpx2

from returns_desk.seeds import Ids
from returns_desk.tracing.langfuse import LangfuseExporter
from tests.conftest import World

REFERENCE = "I ordered two jackets. The blue one arrived damaged. Please refund just that one."
NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")


def read_trace(world: World, user: str, merchant: str, case_id: str) -> dict[str, Any]:
    response = world.call("GET", f"/cases/{case_id}/trace", user=user, merchant=merchant)
    assert response.status_code == 200, response.text
    return response.json()["trace"]


def of_kind(trace: dict[str, Any], *kinds: str) -> list[dict[str, Any]]:
    return [span for span in trace["spans"] if span["kind"] in kinds]


def test_one_correlation_id_runs_from_the_message_to_the_processor(world: World):
    response = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body={"client_request_id": "trace-1", "message": REFERENCE},
    )
    request_id = response.headers["x-request-id"]
    case = response.json()["case"]

    trace = read_trace(world, "sam", "northline", case["id"])

    assert trace["request_ids"] == [request_id]
    assert trace["trace_id"] == case["id"].replace("-", "")

    # The agent turn, with the tools it called in order.
    [turn] = of_kind(trace, "agent")
    assert turn["attributes"]["interpretation"]["intent"] == "refund"
    tools = [s for s in of_kind(trace, "tool", "retriever") if s["parent_id"] == turn["span_id"]]
    assert [t["name"] for t in tools] == ["get_order", "quote_resolution", "submit_resolution"]
    assert tools[2]["attributes"]["outcome"] == "processing"

    # The model attempt, with what it cost in tokens.
    [generation] = of_kind(trace, "generation")
    assert generation["parent_id"] == turn["span_id"]
    assert generation["attributes"]["purpose"] == "interpret"
    assert generation["attributes"]["outcome"] == "ok"

    # State changes and the processor call.
    assert [e["name"] for e in of_kind(trace, "event")] == [
        "case_opened",
        "refund_authorized",
        "refund_confirmed",
    ]
    [processor_call] = [s for s in trace["spans"] if s["name"] == "processor.create_refund"]
    assert processor_call["attributes"]["outcome"] == "succeeded"

    # One ID on every span the request produced, and on the processor's own record.
    carried = {s["attributes"]["request_id"] for s in (turn, *tools, generation, processor_call)}
    assert carried == {request_id}
    [refund] = world.processor.list_refunds(NORTHLINE_PAYMENT)
    assert refund.metadata["request_id"] == request_id
    assert processor_call["attributes"]["refund_id"] == refund.id


def test_time_waiting_for_a_supervisor_is_reported_apart_from_service_time(world: World):
    case = world.say("maya", "vale", REFERENCE)
    assert case["state"] == "awaiting_supervisor"

    world.clock.advance(timedelta(hours=2))
    review_id = world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"][0][
        "review_id"
    ]
    decided = world.call(
        "POST",
        f"/reviews/{review_id}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve", "note": "Photo shows the damage."},
    )
    assert decided.status_code == 200

    trace = read_trace(world, "vera", "vale", case["id"])

    assert trace["state"] == "resolved"
    assert len(trace["request_ids"]) == 2  # the shopper's request and the supervisor's
    [wait] = of_kind(trace, "wait")
    assert wait["name"] == "waiting for supervisor"
    assert trace["totals"]["human_wait_seconds"] == {"supervisor": 7200.0}
    # Two hours of waiting, and well under a minute of the service actually working.
    assert trace["totals"]["active_ms"] < 60_000
    # Both turns are there: the one that paused and the one the decision resumed.
    assert [s["attributes"]["outcome"] for s in of_kind(trace, "agent")] == [
        "pending_review",
        "review_approved",
    ]


def test_a_trace_carries_ids_and_codes_but_no_text(world: World):
    question = "How long do I have to return things?"
    first = world.say("maya", "northline", question)
    second = world.say("maya", "northline", question)

    trace = read_trace(world, "nina", "northline", first["id"])
    [search] = of_kind(trace, "retriever")
    assert search["name"] == "search_policy"
    assert search["attributes"]["cache"] == "miss"
    assert len(search["attributes"]["evidence_ids"]) >= 1
    # The same question again is served from the cache, after its access was rechecked.
    [again] = of_kind(read_trace(world, "nina", "northline", second["id"]), "retriever")
    assert again["attributes"]["cache"] == "hit"
    assert again["attributes"]["evidence_ids"] == search["attributes"]["evidence_ids"]

    dumped = json.dumps(trace)
    assert question not in dumped
    assert "You can return an item within" not in dumped
    assert "days of delivery" not in dumped


def test_a_reader_sees_what_their_role_may_see_today(world: World):
    case = world.say("maya", "vale", REFERENCE)
    path = f"/cases/{case['id']}/trace"

    # The shopper has no trace view, and another merchant's staff cannot find the case.
    assert world.call("GET", path, user="maya", merchant="vale").status_code == 403
    assert world.call("GET", path, user="sam", merchant="northline").status_code == 404
    assert world.call("GET", path).status_code == 401

    as_supervisor = read_trace(world, "vera", "vale", case["id"])
    assert as_supervisor["review"][0]["reasons"] == ["above_auto_limit"]
    assert "auto_approve_limit_cents" in as_supervisor["review"][0]["internal_evidence"]

    # An associate reads the same run without the restricted review record.
    as_associate = read_trace(world, "alex", "vale", case["id"])
    assert "review" not in as_associate
    assert "above_auto_limit" not in json.dumps(as_associate)
    assert "auto_approve_limit" not in json.dumps(as_associate)
    assert [s["name"] for s in as_associate["spans"]] == [s["name"] for s in as_supervisor["spans"]]

    # A supervisor who is made an associate loses it on this historic trace as well.
    with world.admin() as conn:
        conn.execute(
            "update memberships set role = 'associate' where user_id = %s", (Ids.user("vera"),)
        )
    downgraded = read_trace(world, "vera", "vale", case["id"])
    assert "review" not in downgraded
    assert "above_auto_limit" not in json.dumps(downgraded)


# --- export to Langfuse -------------------------------------------------------------------


class FakeLangfuse:
    """Receives OTLP exports. `fail_next` makes the next export fail."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.fail_next = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.fail_next:
            self.fail_next = False
            return httpx2.Response(503, json={"message": "try later"})
        return httpx2.Response(200, json={})

    def spans(self, index: int = -1) -> list[dict[str, Any]]:
        body = json.loads(self.requests[index].content)
        return body["resourceSpans"][0]["scopeSpans"][0]["spans"]

    def connect(self, world: World) -> None:
        settings = world.settings.model_copy(
            update={
                "langfuse_public_key": "pk-lf-test",
                "langfuse_secret_key": "sk-lf-test",
                "langfuse_host": "https://langfuse.test",
                "langfuse_project_id": "proj_1",
            }
        )
        world.container.tracer = LangfuseExporter(
            world.container.db, settings, transport=httpx2.MockTransport(self)
        )


def attribute(span: dict[str, Any], key: str) -> Any:
    for item in span["attributes"]:
        if item["key"] == key:
            return next(iter(item["value"].values()))
    return None


def test_spans_are_sent_to_langfuse_once_as_opentelemetry(world: World):
    langfuse = FakeLangfuse()
    langfuse.connect(world)

    case = world.say("maya", "northline", REFERENCE)

    # The export ran after the response, without being asked.
    [request] = langfuse.requests
    assert request.url.path == "/api/public/otel/v1/traces"
    assert request.headers["authorization"] == "Basic " + (
        base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    )
    assert request.headers["x-langfuse-ingestion-version"] == "4"

    spans = langfuse.spans()
    assert {s["traceId"] for s in spans} == {case["id"].replace("-", "")}
    ids = [s["spanId"] for s in spans]
    assert len(ids) == len(set(ids)) and all(len(i) == 16 for i in ids)
    # Every parent that is named was sent too.
    assert {s["parentSpanId"] for s in spans if "parentSpanId" in s} <= set(ids)
    assert all(int(s["endTimeUnixNano"]) >= int(s["startTimeUnixNano"]) for s in spans)

    types = [attribute(s, "langfuse.observation.type") for s in spans]
    assert types.count("agent") == 1 and types.count("generation") == 1
    assert types.count("tool") == 4  # three tool calls and the processor call
    [generation] = [s for s in spans if attribute(s, "langfuse.observation.type") == "generation"]
    assert attribute(generation, "langfuse.observation.model.name") == "scripted"
    assert json.loads(attribute(generation, "langfuse.observation.usage_details")) == {
        "input": 0,
        "output": 0,
    }
    assert attribute(generation, "langfuse.trace.metadata.merchant") == "northline"
    assert REFERENCE not in request.content.decode()

    # Staff get a link to it on the case.
    view = world.call("GET", f"/cases/{case['id']}", user="sam", merchant="northline").json()
    assert view["case"]["trace"]["langfuse_url"] == (
        f"https://langfuse.test/project/proj_1/traces/{case['id'].replace('-', '')}"
    )

    # Nothing is sent twice.
    assert world.container.tracer.export_new() == {"spans": 0}
    assert len(langfuse.requests) == 1


def test_a_failed_export_is_retried_with_the_same_spans(world: World):
    langfuse = FakeLangfuse()
    langfuse.connect(world)
    langfuse.fail_next = True

    case = world.say("maya", "northline", REFERENCE)

    # The shopper was served regardless, and nothing was marked as sent.
    assert case["state"] == "resolved"
    assert world.rows("select 1 from trace_exports") == []
    first_try = {s["spanId"] for s in langfuse.spans(0)}

    result = world.container.tracer.export_new()

    assert result["spans"] == len(first_try)
    assert {s["spanId"] for s in langfuse.spans(1)} == first_try
    assert world.container.tracer.export_new() == {"spans": 0}


def test_without_keys_nothing_is_sent_and_there_is_no_link(world: World):
    case = world.say("maya", "northline", REFERENCE)

    assert world.container.tracer.enabled is False
    assert world.container.tracer.export_new() == {"spans": 0}
    view = world.call("GET", f"/cases/{case['id']}", user="sam", merchant="northline").json()
    assert view["case"]["trace"]["langfuse_url"] is None
    assert view["case"]["trace"]["path"] == f"/cases/{case['id']}/trace"
    # A shopper's view of the case says nothing about traces.
    mine = world.call("GET", f"/cases/{case['id']}", user="maya", merchant="northline").json()
    assert "trace" not in mine["case"]
