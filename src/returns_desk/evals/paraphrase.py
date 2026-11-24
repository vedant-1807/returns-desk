"""LLM-written phrasings for the eval templates, kept only when they preserve the facts.

A model rewrites each template several ways: casual, formal, terse, with typos. A model
can also quietly change what a message means, which would make the ground truth wrong. So
every rewrite is checked by rule before it is kept: the placeholders are intact, a clear
request is still a clear request and a question still a question, the reason is still the
reason, and for an exchange the wanted size is still the wanted size. What fails is
dropped. The accepted rewrites are saved in the repository, so the suite stays the same
from run to run and needs no model to generate.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from returns_desk.agent import language
from returns_desk.models.gateway import ModelGateway, ModelRequest

PROMPT_VERSION = "1"
PARAPHRASE_FILE = Path(__file__).parent / "data" / "paraphrases.json"

_RULES = """\
You rewrite a message that a shopper might send to a store's returns desk.

Write {n} different versions of the message. Keep the meaning exactly the same: the same \
request, the same item, the same reason, and the same level of certainty. A question must \
stay a question. A clear request must stay a clear request. Do not add or remove facts.

Vary the wording, the length and the tone the way real people do: casual, formal, terse, \
chatty, lower case, a small typo now and then.

The message contains placeholders in curly braces, such as {{color}} and {{damage}}. Keep \
every placeholder exactly as written, each exactly once. {{color}} stands for a phrase \
like "blue jacket" and {{damage}} for a phrase like "arrived damaged" or "has a broken \
zip", so each version must read correctly once they are filled in.
"""

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["variants"],
    "properties": {"variants": {"type": "array", "items": {"type": "string"}}},
}

_COLORS = {"blue": ["blue", "blue one", "blue jacket"], "green": ["green jacket"]}
_DAMAGES = ["arrived damaged", "has a broken zip", "came with a torn sleeve", "is defective"]
_VARIANTS = ["Blue / S", "Blue / M", "Blue / L", "Green / M", "Green / L"]
_LINES = [
    {"ref": "blue", "label": "Trail Jacket, Blue / M"},
    {"ref": "green", "label": "Trail Jacket, Green / M"},
]
_EXCHANGE_WORDS = re.compile(r"\b(exchange\w*|swap\w*|replace\w*|replacement)\b")
_REFUND_WORDS = re.compile(r"\b(refund\w*|money back|reimburs\w*)\b")


class _Variants(BaseModel):
    model_config = ConfigDict(extra="ignore")

    variants: list[str]


def _samples(template: str) -> list[tuple[str, str]]:
    """The template filled in every way the suite fills it: (colour, message)."""
    out = []
    for line, phrases in _COLORS.items():
        for phrase in phrases:
            for damage in _DAMAGES if "{damage}" in template else [""]:
                out.append((line, template.format(color=phrase, damage=damage)))
    return out


def accept(kind: str, template: str) -> str | None:
    """None when the rewrite may be kept, otherwise the reason it is dropped."""
    wants_color = kind != "ambiguous"
    wants_damage = kind not in ("no_reason", "changed_mind")
    if template.count("{color}") != int(wants_color) or template.count("{damage}") != int(
        wants_damage
    ):
        return "placeholders changed"
    if re.search(r"\{(?!color\}|damage\})", template) or len(template) > 300:
        return "unknown placeholder or too long"
    exchange = kind.startswith("exchange")
    expects_question = kind in ("question", "exchange_question")
    reason = {"no_reason": None, "changed_mind": "changed_mind"}.get(kind, "damaged")

    for line, message in (
        _samples(template) if wants_color else [("", template.format(damage=d)) for d in _DAMAGES]
    ):
        lowered = message.lower()
        if language.find_reason(message) != reason:
            return f"reason is no longer {reason}"
        if exchange and _REFUND_WORDS.search(lowered):
            return "an exchange that now mentions a refund"
        if not exchange and _EXCHANGE_WORDS.search(lowered):
            return "a refund that now mentions an exchange"
        if expects_question and "?" not in message:
            return "a question became a statement"
        if not expects_question and language.is_capability_question(message):
            return "a clear request became a question"
        if expects_question and language.has_request_marker(message):
            return "a question became a clear request"
        if wants_color:
            refs, distinct = language.line_evidence(message, _LINES)
            if not (distinct and refs == [line]):
                return "the item is no longer clear"
        else:
            if language.line_evidence(message, _LINES)[1]:
                return "an ambiguous message now names the item"
        if exchange:
            wanted = language.wanted_variant(message, f"{line.title()} / M", _VARIANTS)
            size = "S" if kind == "exchange_unavailable" else "L"
            expected_how = "unknown" if (line, size) == ("green", "S") else "named"
            if wanted != (f"{line.title()} / {size}", expected_how):
                return "the wanted size changed"
    return None


def paraphrase_request(template: str, n: int) -> ModelRequest:
    return ModelRequest(
        purpose="paraphrase",
        system=_RULES.format(n=n),
        user=f'Message to rewrite:\n"""\n{template}\n"""',
        schema_name="rewrites",
        schema=_SCHEMA,
        tier="strong",
        max_output_tokens=700,
        inputs={"template": template},
    )


def build(
    gateway: ModelGateway, templates: dict[str, list[str]], *, variants: int, label: str
) -> dict[str, Any]:
    """Ask for rewrites of every template and keep the ones that pass the checks."""
    accepted: dict[str, list[str]] = {}
    dropped: list[dict[str, str]] = []
    failed_calls = 0
    for kind, originals in templates.items():
        kept: list[str] = []
        for original in originals:
            result = gateway.complete(
                paraphrase_request(original, variants),
                gateway.new_budget(),
                _Variants.model_validate,
            )
            if not result.ok:
                failed_calls += 1
                continue
            for text in _Variants.model_validate(result.data).variants:
                text = " ".join(text.split())
                if not text or text in originals or text in kept:
                    continue
                try:
                    problem = accept(kind, text)
                except (KeyError, IndexError, ValueError):
                    problem = "broken placeholders"
                if problem is None:
                    kept.append(text)
                else:
                    dropped.append({"kind": kind, "text": text, "why": problem})
        accepted[kind] = kept
    return {
        "about": "Rewrites of the eval templates written by a model and kept only when "
        "rule checks show they preserve the facts of the original.",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "model": label,
        "prompt_version": PROMPT_VERSION,
        "accepted": accepted,
        "accepted_count": sum(len(v) for v in accepted.values()),
        "dropped_count": len(dropped),
        "failed_calls": failed_calls,
        "dropped": dropped,
    }


def load() -> dict[str, list[str]]:
    """The accepted rewrites, by template kind. Empty when none have been generated."""
    if not PARAPHRASE_FILE.exists():
        return {}
    return json.loads(PARAPHRASE_FILE.read_text()).get("accepted", {})
