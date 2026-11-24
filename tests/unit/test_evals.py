"""The eval tooling itself: the judge, its calibration, and the rewrite checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from returns_desk.evals import cases, paraphrase
from returns_desk.evals.judge import Judge, calibrate, cohens_kappa, load_calibration
from returns_desk.models.gateway import ModelRequest, ScriptedGateway


def test_kappa_is_agreement_corrected_for_chance():
    assert cohens_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    # Three of four agree; half of that is what chance alone would give.
    assert cohens_kappa([True, True, False, False], [True, False, False, False]) == 0.5
    # A judge that says yes to everything agrees half the time and has learned nothing.
    assert cohens_kappa([True, False, True, False], [True, True, True, True]) == 0.0
    with pytest.raises(ValueError):
        cohens_kappa([], [])


def test_the_hand_labelled_set_is_balanced_and_well_formed():
    examples = load_calibration()
    assert len(examples) == 40
    assert sum(e["faithful"] for e in examples) == 20
    assert len({e["id"] for e in examples}) == 40
    assert all(e["passages"] and e["answer"] and e["question"] for e in examples)


def scripted_judge(say: Any, tmp_path: Path) -> tuple[Judge, ScriptedGateway]:
    gateway = ScriptedGateway(say)
    return Judge(gateway, "test-model", tmp_path / "cache.json"), gateway


def test_a_verdict_is_asked_for_once_and_then_read_from_the_cache(tmp_path: Path):
    judge, gateway = scripted_judge(lambda r: {"faithful": False, "unsupported": ["x"]}, tmp_path)
    passages = [{"title": "Return window", "text": "30 days."}]

    assert judge.judge("How long?", "60 days.", passages) is False
    assert judge.judge("How long?", "60 days.", passages) is False
    assert (len(gateway.requests), judge.calls, judge.cache_hits) == (1, 1, 1)

    # A new judge, as in a later run, finds the verdict on disk.
    later, later_gateway = scripted_judge(lambda r: {"faithful": True, "unsupported": []}, tmp_path)
    assert later.judge("How long?", "60 days.", passages) is False
    assert later_gateway.requests == []
    # A different answer is a different question for the judge.
    assert later.judge("How long?", "30 days.", passages) is True


def test_a_judge_that_gives_no_verdict_is_reported_not_guessed(tmp_path: Path):
    judge, _ = scripted_judge(lambda r: TimeoutError("down"), tmp_path)
    assert judge.judge("q", "a", [{"title": "t", "text": "x"}]) is None
    result = calibrate(judge, load_calibration()[:4])
    assert (result["judged"], result["unjudged"], result["cohens_kappa"]) == (0, 4, None)


def test_calibration_measures_the_judge_against_the_labels(tmp_path: Path):
    examples = load_calibration()
    truth = {e["answer"]: e["faithful"] for e in examples}
    wrong = examples[0]["answer"]

    def mostly_right(request: ModelRequest) -> dict[str, Any]:
        answer = request.inputs["answer"]
        verdict = truth[answer] if answer != wrong else not truth[answer]
        return {"faithful": verdict, "unsupported": []}

    judge, _ = scripted_judge(mostly_right, tmp_path)
    result = calibrate(judge, examples)

    assert result["judged"] == 40 and result["agreement"] == 0.975
    assert result["cohens_kappa"] == 0.95
    assert result["disagreements"] == [{"id": "J01", "label": True, "judge": False}]


@pytest.mark.parametrize(
    ("kind", "text", "kept"),
    [
        ("explicit", "pls refund my {color}, it {damage}!!", True),
        ("explicit", "My {color} {damage}. Can you refund it please?", True),
        # A clear request turned into a question.
        ("explicit", "Can I get a refund for the {color}? It {damage}.", False),
        # A placeholder was dropped, or invented.
        ("explicit", "Please refund the {color}.", False),
        ("explicit", "Please refund the {colour}, it {damage}.", False),
        # The reason changed.
        ("explicit", "Please refund the {color}, I just do not like it. It {damage}.", True),
        ("no_reason", "Please refund the {color}, it came broken.", False),
        ("changed_mind", "Refund the {color} please, I changed my mind.", True),
        # A question must stay one.
        ("question", "Is it possible to return the {color}? It {damage}.", True),
        ("question", "Please refund the {color}. It {damage}.", False),
        # An ambiguous message must not name the item.
        ("ambiguous", "my jacket {damage}, refund please", True),
        ("ambiguous", "my blue jacket {damage}, refund please", False),
        # An exchange keeps its size, and stays an exchange.
        ("exchange_explicit", "My {color} {damage}. Please swap it for a size L.", True),
        ("exchange_explicit", "My {color} {damage}. Please swap it for a medium.", False),
        ("exchange_explicit", "My {color} {damage}. Please refund it or swap for a large.", False),
        ("exchange_unavailable", "The {color} {damage}. Please exchange it for a small.", True),
    ],
)
def test_a_rewrite_is_kept_only_if_it_preserves_the_facts(kind: str, text: str, kept: bool):
    assert (paraphrase.accept(kind, text) is None) is kept, paraphrase.accept(kind, text)


def test_every_hand_written_template_passes_its_own_check():
    for kind, templates in cases.TEMPLATES.items():
        for template in templates:
            assert paraphrase.accept(kind, template) is None, (kind, template)


def test_building_rewrites_keeps_the_good_and_records_the_dropped():
    def write(request: ModelRequest) -> dict[str, Any]:
        return {
            "variants": [
                "hey, the {color} {damage}, refund it pls",
                "Can I get my money back for the {color}? It {damage}.",
                request.inputs["template"],  # an unchanged copy is not a rewrite
            ]
        }

    built = paraphrase.build(
        ScriptedGateway(write),
        {"explicit": cases.TEMPLATES["explicit"][:1]},
        variants=3,
        label="test-model",
    )

    assert built["accepted"] == {"explicit": ["hey, the {color} {damage}, refund it pls"]}
    assert [d["why"] for d in built["dropped"]] == ["a clear request became a question"]
    assert json.dumps(built)  # it is what gets saved


def test_accepted_rewrites_join_the_suite_and_are_tagged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        paraphrase, "load", lambda: {"explicit": ["yo refund the {color}, it {damage}"]}
    )
    suite = cases.generate(300, seed=7)

    rewritten = [c for c in suite if "llm_paraphrase" in c.tags]
    assert rewritten and all("yo re" in c.turns[0].lower() for c in rewritten)
    # The same seed gives the same suite, rewrites included.
    assert [c.turns for c in suite] == [c.turns for c in cases.generate(300, seed=7)]
