from __future__ import annotations

import pytest

from returns_desk.agent import language
from returns_desk.agent.graph import next_step
from returns_desk.agent.interpret import (
    clean_interpretation,
    interpretation_schema,
    validate_answer,
    validate_interpretation,
)
from returns_desk.models import keyword
from returns_desk.retrieval.embedders import EMBEDDING_DIM, HashEmbedder
from returns_desk.retrieval.index import chunk_text

JACKETS = [
    {
        "ref": "L1",
        "line_id": "blue",
        "label": "Trail Jacket, Blue / M",
        "quantity": 1,
        "remaining_quantity": 1,
        "order_number": "NL-1001",
    },
    {
        "ref": "L2",
        "line_id": "green",
        "label": "Trail Jacket, Green / M",
        "quantity": 1,
        "remaining_quantity": 1,
        "order_number": "NL-1001",
    },
]
MUGS = [
    {
        "ref": "L1",
        "line_id": "mugs",
        "label": "Stoneware Mug, Sand",
        "quantity": 4,
        "remaining_quantity": 4,
        "order_number": "NL-3001",
    },
]


# --- consent veto -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Can I return this?",
        "can i return the jacket",
        "Is it possible to get a refund on the blue one?",
        "Am I still within the return window?",
        "How do I return an item?",
        "What is your return policy?",
        "Do you accept returns on sale items?",
    ],
)
def test_questions_about_what_is_possible_are_never_consent(text: str):
    assert language.is_capability_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "Please refund the blue jacket",
        "The blue one arrived damaged. Please refund just that one.",
        "I want my money back for the green jacket",
        "Can you refund the blue jacket please?",
        "I'd like a refund",
        "refund it",
        "Can I return this? Actually yes, please refund it.",
    ],
)
def test_clear_requests_are_not_vetoed(text: str):
    assert not language.is_capability_question(text)


def test_yes_and_no_replies():
    for text in ("yes", "Yes please", "yep, go ahead", "ok", "Sure", "please do", "confirm"):
        assert language.is_affirmative(text), text
    for text in ("no", "No thanks", "nope", "cancel that", "never mind", "don't"):
        assert language.is_negative(text), text
    # Hedged or mixed replies are neither: they go back through the interpreter.
    for text in ("yes but only the green one", "ok but not yet", "maybe", "what happens then?"):
        assert not language.is_affirmative(text), text


def test_a_yes_or_no_that_says_something_else_is_not_an_answer():
    # These start like a yes or a no and then ask for something different. Reading them as
    # an answer would confirm, or cancel, the wrong thing.
    for text in ("ok, a refund then", "Yes, refund it", "sure, but make it the green one"):
        assert not language.is_affirmative(text), text
    for text in ("No, the green one", "no i want a refund", "no, exchange it instead"):
        assert not language.is_negative(text), text


VARIANTS = ["Blue / S", "Blue / M", "Blue / L", "Green / M", "Green / L"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # An attribute the shopper does not mention stays as it is.
        ("Please exchange it for a large", ("Blue / L", "named")),
        ("Size L please", ("Blue / L", "named")),
        ("swap the blue jacket for the green one", ("Green / M", "named")),
        ("I'd like the green one in large instead", ("Green / L", "named")),
        ("get me a gren one", ("Green / M", "named")),
        # The same item again.
        ("it arrived damaged, please send me a replacement", ("Blue / M", "same")),
        # Named, but not something the product comes in.
        ("I want it in XL", ("Blue / XL", "unknown")),
        ("green and small please", ("Green / S", "unknown")),
        # Nothing to go on.
        ("exchange please", (None, "unclear")),
        ("something else", (None, "unclear")),
    ],
)
def test_which_variant_the_shoppers_words_ask_for(text: str, expected: tuple[str | None, str]):
    assert language.wanted_variant(text, "Blue / M", VARIANTS) == expected


def test_variants_with_a_single_attribute_are_matched_too():
    assert language.wanted_variant("the black one", "Natural", ["Natural", "Black"]) == (
        "Black",
        "named",
    )


def test_quantities_and_reasons():
    assert language.parse_quantity("two") == 2
    assert language.parse_quantity("just 3 please") == 3
    assert language.parse_quantity("all of them") is None
    assert language.stated_quantity("refund two of the mugs") == 2
    assert language.stated_quantity("I paid $500 for order NL-1001") is None
    assert language.find_reason("it came with a torn sleeve") == "damaged"
    assert language.find_reason("you sent me the wrong size") == "wrong_item"
    assert language.find_reason("I changed my mind") == "changed_mind"
    assert language.find_reason("looks different from the picture") == "not_as_described"
    assert language.find_reason("hmm") is None


def test_line_matching_uses_the_words_that_tell_lines_apart():
    lines = [{"ref": c["ref"], "label": c["label"]} for c in JACKETS]
    assert language.match_lines("the blue one", lines) == ["L1"]
    assert language.match_lines("green please", lines) == ["L2"]
    # "jacket" is on both labels, so it identifies neither.
    assert language.match_lines("my jacket is damaged", lines) == ["L1", "L2"]
    assert language.match_lines("the shoes", lines) == []


# --- planning -----------------------------------------------------------------------------


def test_plan_reference_case_goes_straight_to_a_quote():
    known = {"intent": "refund", "line_id": "blue", "reason": "damaged", "explicit": True}
    assert next_step(known, JACKETS) == ("quote", None)
    assert known["quantity"] == 1  # one unit on the line, so nothing to ask


def test_plan_asks_which_line_when_several_fit():
    known = {"intent": "refund", "line_options": ["L1", "L2"], "reason": "damaged"}
    step, question = next_step(known, JACKETS)
    assert step == "clarify"
    assert question["kind"] == "clarify_line"
    assert question["options"] == ["L1", "L2"]
    assert "Blue / M or Trail Jacket, Green / M" in question["question"]


def test_plan_asks_which_item_when_none_is_named():
    step, question = next_step({"intent": "refund"}, JACKETS)
    assert (step, question["kind"]) == ("clarify", "clarify_line")
    assert question["question"].startswith("Which item is this about")


def test_plan_asks_how_many_only_when_the_line_has_several_units():
    known = {"intent": "refund", "line_id": "mugs", "reason": "damaged"}
    step, question = next_step(known, MUGS)
    assert (step, question["kind"]) == ("clarify", "clarify_quantity")
    known["quantity"] = 2
    assert next_step(known, MUGS) == ("quote", None)


def test_plan_picks_the_only_line_without_asking():
    known = {"intent": "refund", "quantity": 1, "reason": "damaged"}
    assert next_step(known, MUGS) == ("quote", None)
    assert known["line_id"] == "mugs"


def test_plan_asks_for_a_missing_reason():
    step, question = next_step({"intent": "refund", "line_id": "blue"}, JACKETS)
    assert (step, question["kind"]) == ("clarify", "clarify_reason")


def test_plan_asks_about_the_action_when_refund_and_exchange_are_mixed():
    for intent in ("refund", "exchange"):
        step, question = next_step({"intent": intent, "wants_both": True}, JACKETS)
        assert (step, question["kind"]) == ("clarify", "clarify_action")


def test_plan_settles_the_replacement_last_for_an_exchange():
    known = {"intent": "exchange"}
    # The item, the quantity and the reason are established exactly as for a refund.
    assert next_step(dict(known), JACKETS)[1]["kind"] == "clarify_line"
    known["line_id"] = "blue"
    assert next_step(dict(known), JACKETS)[1]["kind"] == "clarify_reason"
    known["reason"] = "damaged"
    # Then which variant they want instead, read against the real stock list.
    assert next_step(dict(known), JACKETS) == ("choose_replacement", None)
    known["replacement_variant"] = "Blue / L"
    assert next_step(dict(known), JACKETS) == ("quote", None)


def test_plan_routes_questions_and_unknowns():
    assert next_step({"intent": "policy_question"}, JACKETS) == ("answer_policy", None)
    assert next_step({"intent": "status"}, JACKETS) == ("status", None)
    assert next_step({"intent": "refund"}, []) == ("no_order", None)
    assert next_step({}, JACKETS)[1]["kind"] == "clarify_intent"
    # With no model answer and nothing established, a person takes over.
    assert next_step({}, JACKETS, model_available=False) == ("handoff", None)


# --- what the model may say ---------------------------------------------------------------


def test_the_model_cannot_invent_order_lines_or_fields():
    check = validate_interpretation(["L1", "L2"])
    good = {
        "intent": "refund",
        "lines": ["L1"],
        "quantity": None,
        "reason": None,
        "explicit_request": True,
        "mentions_refund_and_exchange": False,
    }
    assert check(good).lines == ["L1"]
    with pytest.raises(ValueError):
        check({**good, "lines": ["L7"]})
    with pytest.raises(ValueError):
        check({**good, "intent": "approve_refund"})
    with pytest.raises(ValueError):
        check({**good, "quantity": -3})
    # Extra fields a model adds are dropped, not obeyed.
    cleaned = clean_interpretation({**good, "amount_cents": 50000, "role": "supervisor"}, ["L1"])
    assert not hasattr(cleaned, "amount_cents")
    assert "amount_cents" not in cleaned.model_dump()


def test_interpretation_schema_only_offers_real_lines():
    schema = interpretation_schema(["L1", "L2"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["lines"]["items"]["enum"] == ["L1", "L2"]
    assert set(schema["required"]) == set(schema["properties"])


def test_a_policy_answer_must_cite_a_real_passage():
    check = validate_answer(["P1", "P2"])
    assert check({"answer": "14 days.", "cited": ["P1"]}).cited == ["P1"]
    for bad in (
        {"answer": "14 days.", "cited": []},
        {"answer": "x", "cited": ["P9"]},
        {"answer": "", "cited": ["P1"]},
    ):
        with pytest.raises(ValueError):
            check(bad)


# --- the keyword stand-in -----------------------------------------------------------------


def lines_input():
    return [{"ref": c["ref"], "label": c["label"], "quantity": c["quantity"]} for c in JACKETS]


def test_keyword_stand_in_reads_the_reference_sentence():
    out = keyword.interpret(
        {
            "message": "I ordered two jackets. The blue one arrived damaged. "
            "Please refund just that one.",
            "lines": lines_input(),
        }
    )
    assert out["intent"] == "refund"
    assert out["lines"] == ["L1"]
    assert out["reason"] == "damaged"
    assert out["explicit_request"] is True


def test_keyword_stand_in_tells_questions_from_requests():
    asked = keyword.interpret({"message": "Can I return the blue jacket?", "lines": lines_input()})
    assert (asked["intent"], asked["explicit_request"]) == ("refund", False)
    general = keyword.interpret(
        {"message": "How long do I have to return things?", "lines": lines_input()}
    )
    assert general["intent"] == "policy_question"
    status = keyword.interpret({"message": "Where is my refund?", "lines": lines_input()})
    assert status["intent"] == "status"


# --- embeddings ---------------------------------------------------------------------------


def test_hash_embedder_is_deterministic_normalized_and_lexical():
    embedder = HashEmbedder()
    a = embedder.embed_query("how long do I have to return an item")
    b = embedder.embed_query("how long do I have to return an item")
    assert a == b and len(a) == EMBEDDING_DIM
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9

    def cosine(x, y):
        return sum(p * q for p, q in zip(x, y, strict=True))

    window = embedder.embed_documents(["Return window\nYou can return an item within 14 days"])[0]
    limit = embedder.embed_documents(["Approval limit\nRefunds above $200 need a supervisor"])[0]
    assert cosine(a, window) > cosine(a, limit)
    # A text with no usable words still gets a valid vector.
    empty = embedder.embed_query("???")
    assert abs(sum(x * x for x in empty) - 1.0) < 1e-9


def test_chunking_keeps_short_sections_whole_and_splits_long_ones():
    assert chunk_text("One short paragraph.") == ["One short paragraph."]
    long_text = "\n\n".join(f"Paragraph {i}. " + "word " * 80 for i in range(6))
    chunks = chunk_text(long_text, max_chars=900)
    assert len(chunks) > 1
    assert all(len(chunk) <= 900 + 500 for chunk in chunks)
    assert "".join(chunks).count("Paragraph") == 6
