"""Small, deterministic readers of shopper text.

These are not the interpreter: the model is. They exist for three narrow jobs where a rule
is safer than a guess:

* a veto on consent: a question about what is possible is never a request to do it,
* short replies to a question we just asked ("yes", "no", "two"),
* a fallback when no model answer is available.
"""

from __future__ import annotations

import difflib
import re

_WORD = re.compile(r"[a-z0-9']+")

# "Can I return this?" asks. "Can you refund this?" tells. Only the first kind is listed.
_CAPABILITY_QUESTION = re.compile(
    r"\b("
    r"can i|could i|may i|am i able|am i allowed|is it possible|would it be possible|"
    r"do you (accept|allow|take|offer)|are returns|is there (a|any)|"
    r"how (do|can|would) i|how long|how many days|what('s| is| are) (the|your)|"
    r"am i (still )?(eligible|within|in time)|is it too late"
    r")\b"
)
_REQUEST_MARKER = re.compile(
    r"\b("
    r"please (refund|return|send|process|issue)|"
    r"refund (it|this|that|them|the|my|just|me|both)|"
    r"(i|we) (want|need|would like|'d like|demand|expect) (a|my|the|to|you to)|"
    r"(i|we)'d like (a|my|the|to)|"
    r"give me (a|my|the) refund|(my|the) money back|"
    r"(go ahead|do it|process (it|the|my|a) refund|issue (a|the|my) refund)|"
    r"can you (please )?(refund|return|process|issue)|"
    r"could you (please )?(refund|return|process|issue)"
    r")\b"
)

_AFFIRMATIVE = re.compile(
    r"^\W*(yes|yeah|yep|yup|sure|ok|okay|please do|go ahead|do it|confirm|confirmed|"
    r"i confirm|that's right|that is right|correct|absolutely|sounds good|proceed)\b"
)
_NEGATIVE = re.compile(
    r"^\W*(no|nope|nah|don't|do not|cancel|stop|never mind|nevermind|not now|leave it)\b"
)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "an": 1, "single": 1, "both": 2,
}  # fmt: skip

_REASONS: list[tuple[str, re.Pattern[str]]] = [
    (
        "damaged",
        re.compile(
            r"\b(damag\w*|broken|broke|torn|ripped|rip|tear|cracked|defect\w*|faulty|"
            r"stain\w*|scratch\w*|hole|holes|busted|doesn't work|does not work|not working)\b"
        ),
    ),
    (
        "wrong_item",
        re.compile(
            r"\b(wrong (item|size|colou?r|one|product|thing)|not what i ordered|"
            r"sent (me )?the wrong|received the wrong|incorrect (item|size))\b"
        ),
    ),
    (
        "not_as_described",
        re.compile(
            r"\b(not as described|not as advertised|not as pictured|"
            r"different (from|than) the (picture|photo|description|listing))\b"
        ),
    ),
    (
        "changed_mind",
        re.compile(
            r"\b(changed my mind|don't want|do not want|no longer (want|need)|"
            r"don't need|do not need|didn't like|did not like|don't like|do not like)\b"
        ),
    ),
]


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def is_capability_question(text: str) -> bool:
    """True when the text asks whether something is possible and does not also clearly
    ask for it to be done. Such a message is never consent."""
    lowered = text.lower()
    return bool(_CAPABILITY_QUESTION.search(lowered)) and not _REQUEST_MARKER.search(lowered)


def has_request_marker(text: str) -> bool:
    return bool(_REQUEST_MARKER.search(text.lower()))


def is_affirmative(text: str) -> bool:
    lowered = text.lower().strip()
    return bool(_AFFIRMATIVE.match(lowered)) and not re.search(r"\b(but|not|don't)\b", lowered)


def is_negative(text: str) -> bool:
    return bool(_NEGATIVE.match(text.lower().strip()))


def parse_quantity(text: str) -> int | None:
    """A unit count stated in a short reply such as "two" or "just 1"."""
    tokens = words(text)
    for token in tokens:
        if token.isdigit() and 0 < int(token) <= 100:
            return int(token)
    for token in tokens:
        if token in _NUMBER_WORDS and token not in ("a", "an"):
            return _NUMBER_WORDS[token]
    return None


def stated_quantity(text: str) -> int | None:
    """A unit count stated inside a longer message, such as "two of the mugs". Stricter than
    parse_quantity so that prices and order numbers are not mistaken for counts."""
    lowered = text.lower()
    match = re.search(
        r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(of|units?|pairs?|pieces?|items?)\b",
        lowered,
    )
    if not match:
        match = re.search(r"\b(just|only)\s+(\d{1,2}|one|two|three|four|five)\b", lowered)
        if not match:
            return None
        raw = match.group(2)
    else:
        raw = match.group(1)
    return int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)


def find_reason(text: str) -> str | None:
    lowered = text.lower()
    for reason, pattern in _REASONS:
        if pattern.search(lowered):
            return reason
    return None


def _same_word(token: str, word: str) -> bool:
    """Equal, or close enough to be the same word with a typo ("jaket", "gren")."""
    if token == word:
        return True
    if len(token) < 4 or len(word) < 4 or abs(len(token) - len(word)) > 2:
        return False
    return difflib.SequenceMatcher(None, token, word).ratio() >= 0.8


def _hits(tokens: list[str], vocabulary: set[str]) -> bool:
    return any(_same_word(token, word) for token in tokens for word in vocabulary)


def line_evidence(text: str, lines: list[dict[str, str]]) -> tuple[list[str], bool]:
    """Which candidate lines the shopper's own words point at, and how firmly.

    Each line is {"ref", "label"}. Returns (refs, distinct). `distinct` is True when the
    text uses a word that tells the lines apart ("blue"). A word found on every label
    ("jacket") fits them all and identifies none, so it gives distinct=False.
    """
    if not lines:
        return [], False
    tokens = words(text)
    label_words = {line["ref"]: set(words(line["label"])) - _FILLER for line in lines}
    shared = set.intersection(*label_words.values()) if len(lines) > 1 else set()
    distinct_hits = [ref for ref, label in label_words.items() if _hits(tokens, label - shared)]
    if distinct_hits:
        return distinct_hits, True
    generic_hits = [ref for ref, label in label_words.items() if _hits(tokens, label)]
    return generic_hits, False


def match_lines(text: str, lines: list[dict[str, str]]) -> list[str]:
    """The lines a text could mean, by its own words. See line_evidence."""
    return line_evidence(text, lines)[0]


_DAMAGE_WORDS = {
    "damaged", "damage", "broken", "torn", "ripped", "defective", "faulty", "stained",
    "scratched", "cracked", "busted",
}  # fmt: skip


def reason_supported(text: str, reason: str) -> bool:
    """Do the shopper's own words back the reason the model reported?

    The reason decides whether a refund is immediate or reviewed, so a reason the model
    reports is only taken at face value when the text supports it.
    """
    if find_reason(text) == reason:
        return True
    return reason == "damaged" and _hits(words(text), _DAMAGE_WORDS)


_FILLER = frozenset({"m", "s", "l", "xl", "the", "a", "an", "of", "and", "one", "size"})
