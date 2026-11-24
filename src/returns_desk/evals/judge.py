"""An LLM judge for citation faithfulness, and its calibration.

A policy answer is faithful when every factual claim in it is stated in the passages it
cites. Whether that holds is a reading task, so a model judges it. A judge is only worth
its agreement with people, so it is first run over a set of answers that were labelled by
hand and its agreement with those labels is reported as Cohen's kappa. The kappa goes
into every report that uses the judge.

Verdicts are cached per answer, model and prompt version, so a rerun does not spend a
free plan's daily allowance twice.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from returns_desk.models.gateway import ModelGateway, ModelRequest

PROMPT_VERSION = "1"
CALIBRATION_FILE = Path(__file__).parent / "data" / "judge_calibration.json"

_RULES = """\
You check a reply that a store's returns desk gave to a shopper against the policy \
passages the reply cites.

faithful is true only when every factual claim in the reply is stated in the passages. \
A reply may leave things out and may use its own words. It is not faithful if it:
- states a number, a deadline or a condition the passages do not state, or states one \
differently,
- promises an outcome, an amount or a timing the passages do not promise,
- adds a rule the passages do not contain (a fee, a requirement, an exception).

A reply that says it could not find the answer in the policy is faithful. Judge only \
against the passages given, not against what stores usually do.

unsupported: quote each claim in the reply that the passages do not support. Use [] when \
faithful is true.
"""

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["faithful", "unsupported"],
    "properties": {
        "faithful": {"type": "boolean"},
        "unsupported": {"type": "array", "items": {"type": "string"}},
    },
}


class Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    faithful: bool
    unsupported: list[str] = []


def judge_request(question: str, answer: str, passages: list[dict[str, str]]) -> ModelRequest:
    listing = "\n\n".join(f"{p['title']}:\n{p['text']}" for p in passages)
    return ModelRequest(
        purpose="judge",
        system=_RULES,
        user=(
            f'Shopper question:\n"""\n{question}\n"""\n\n'
            f"Cited policy passages:\n\n{listing}\n\n"
            f'Reply to check:\n"""\n{answer}\n"""'
        ),
        schema_name="faithfulness_verdict",
        schema=_SCHEMA,
        tier="strong",
        max_output_tokens=400,
        inputs={"question": question, "answer": answer, "passages": passages},
    )


class Judge:
    def __init__(self, gateway: ModelGateway, label: str, cache_path: Path | None = None) -> None:
        self._gateway = gateway
        # What identifies the judging model in the cache, such as its routes and models.
        self._label = label
        self._cache_path = cache_path
        self._cache: dict[str, Any] = {}
        if cache_path and cache_path.exists():
            self._cache = json.loads(cache_path.read_text())
        self.calls = 0
        self.cache_hits = 0

    def judge(self, question: str, answer: str, passages: list[dict[str, str]]) -> bool | None:
        """True or False, or None when no usable verdict came back within the budget."""
        key = hashlib.sha256(
            json.dumps(
                [PROMPT_VERSION, self._label, question, answer, passages], sort_keys=True
            ).encode()
        ).hexdigest()
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]["faithful"]
        self.calls += 1
        result = self._gateway.complete(
            judge_request(question, answer, passages),
            self._gateway.new_budget(),
            Verdict.model_validate,
        )
        if not result.ok:
            return None
        verdict = Verdict.model_validate(result.data)
        self._cache[key] = {"faithful": verdict.faithful, "unsupported": verdict.unsupported}
        if self._cache_path:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True))
        return verdict.faithful


def cohens_kappa(first: list[bool], second: list[bool]) -> float:
    """Agreement between two raters on yes/no labels, corrected for chance agreement."""
    if len(first) != len(second) or not first:
        raise ValueError("kappa needs two label lists of the same, non-zero length")
    n = len(first)
    observed = sum(a == b for a, b in zip(first, second, strict=True)) / n
    yes_a, yes_b = sum(first) / n, sum(second) / n
    expected = yes_a * yes_b + (1 - yes_a) * (1 - yes_b)
    return 1.0 if expected == 1 else round((observed - expected) / (1 - expected), 4)


def load_calibration() -> list[dict[str, Any]]:
    return json.loads(CALIBRATION_FILE.read_text())["examples"]


def calibrate(judge: Judge, examples: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run the judge over the hand-labelled set and measure its agreement with the labels."""
    examples = examples if examples is not None else load_calibration()
    labels: list[bool] = []
    verdicts: list[bool] = []
    disagreements = []
    unjudged = 0
    for example in examples:
        verdict = judge.judge(example["question"], example["answer"], example["passages"])
        if verdict is None:
            unjudged += 1
            continue
        labels.append(example["faithful"])
        verdicts.append(verdict)
        if verdict != example["faithful"]:
            disagreements.append(
                {"id": example["id"], "label": example["faithful"], "judge": verdict}
            )
    return {
        "prompt_version": PROMPT_VERSION,
        "examples": len(examples),
        "judged": len(labels),
        "unjudged": unjudged,
        "agreement": round(
            sum(a == b for a, b in zip(labels, verdicts, strict=True)) / len(labels), 4
        )
        if labels
        else None,
        "cohens_kappa": cohens_kappa(labels, verdicts) if labels else None,
        "label_faithful": sum(labels),
        "judge_faithful": sum(verdicts),
        "disagreements": disagreements,
    }
