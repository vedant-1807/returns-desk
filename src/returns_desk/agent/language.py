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
    r"can you (please )?(refund|return|process|issue|exchange|swap|replace|send)|"
    r"could you (please )?(refund|return|process|issue|exchange|swap|replace|send)|"
    r"please (exchange|swap|replace)|send me (a|an|the|another)"
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
        "does_not_fit",
        re.compile(
            r"\b(too (small|big|large|tight|loose|long|short)|doesn't fit|does not fit|"
            r"didn't fit|did not fit|wrong fit|bad fit|runs (small|large|big))\b"
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


# A reply counts as a plain yes or a plain no only when it says nothing else. "Ok, a refund
# then" starts like a yes and asks for something different; "No, the green one" starts like
# a no and is a correction. Both are read as new messages, never as an answer.
_YES_VOCABULARY = frozenset(
    [
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "please",
        "do",
        "it",
        "go",
        "ahead",
        "confirm",
        "confirmed",
        "i",
        "that's",
        "that",
        "is",
        "right",
        "correct",
        "absolutely",
        "definitely",
        "sounds",
        "good",
        "great",
        "fine",
        "perfect",
        "proceed",
        "thanks",
        "thank",
        "you",
        "then",
        "of",
        "course",
        "so",
    ]
)
_NO_VOCABULARY = frozenset(
    [
        "no",
        "nope",
        "nah",
        "don't",
        "dont",
        "do",
        "not",
        "cancel",
        "stop",
        "never",
        "mind",
        "nevermind",
        "now",
        "leave",
        "it",
        "thanks",
        "thank",
        "you",
        "please",
        "that",
        "this",
    ]
)


def is_affirmative(text: str) -> bool:
    """A plain yes, and nothing but a yes."""
    lowered = text.lower().strip()
    tokens = words(lowered)
    return (
        bool(_AFFIRMATIVE.match(lowered))
        and len(tokens) <= 8
        and all(token in _YES_VOCABULARY for token in tokens)
    )


def is_negative(text: str) -> bool:
    """A plain no, and nothing but a no."""
    lowered = text.lower().strip()
    tokens = words(lowered)
    return (
        bool(_NEGATIVE.match(lowered))
        and len(tokens) <= 8
        and all(token in _NO_VOCABULARY for token in tokens)
    )


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


# --- exchanges: which variant does the shopper want instead? -----------------------------

# How shoppers say a size. The key is the size as a catalogue writes it.
_SIZE_WORDS: dict[str, tuple[str, ...]] = {
    "xs": ("xs", "extra small", "x-small"),
    "s": ("s", "small"),
    "m": ("m", "medium"),
    "l": ("l", "large"),
    "xl": ("xl", "extra large", "x-large"),
    "xxl": ("xxl", "2xl"),
}
_SAME_AGAIN = re.compile(
    r"\b(same (one|item|thing|size|colou?r|jacket|again)|another one|a new one|"
    r"(a|the) replacement|replace it|send (me )?(a|another|a new) one)\b"
)


def _mentions(text: str, tokens: list[str], value: str) -> bool:
    """Does the text name this attribute value? Sizes have spoken forms ("large" for L);
    anything else is matched as a word, with room for a typo."""
    forms = _SIZE_WORDS.get(value)
    if forms is None:
        return _hits(tokens, {value})
    for form in forms:
        if " " in form or "-" in form:
            if form in text:
                return True
        elif form in tokens:
            return True
    return False


def wanted_variant(text: str, current: str, known: list[str]) -> tuple[str | None, str]:
    """Which variant of a product the shopper's own words ask for in an exchange.

    `current` is the variant they have, such as "Blue / M"; `known` is every variant the
    product comes in. An attribute they do not mention stays as it is, so "a large" on a
    blue medium means "Blue / L". Returns (variant, how):

        named     the words pick out one known variant
        same      the words ask for the same item again
        unknown   the words name a combination the product does not come in
        unclear   the words name no attribute, or more than one for the same attribute
    """
    lowered = text.lower()
    tokens = words(lowered)
    have = [part.strip().lower() for part in current.split("/")]
    catalogue = {
        variant: [part.strip().lower() for part in variant.split("/")] for variant in known
    }
    if any(len(parts) != len(have) for parts in catalogue.values()):
        # Variants that do not share a shape cannot be compared attribute by attribute.
        return None, "unclear"

    want = list(have)
    changed = False
    for position, mine in enumerate(have):
        values = {parts[position] for parts in catalogue.values()} | {mine}
        if values <= set(_SIZE_WORDS):
            # A size the product does not come in is still a size the shopper named.
            values |= set(_SIZE_WORDS)
        named = {value for value in values if _mentions(lowered, tokens, value)}
        # Naming what they have ("the blue jacket") describes the item, not the wish.
        named.discard(mine)
        if len(named) > 1:
            return None, "unclear"
        if named:
            want[position] = named.pop()
            changed = True

    if not changed:
        return (current, "same") if _SAME_AGAIN.search(lowered) else (None, "unclear")
    for variant, parts in catalogue.items():
        if parts == want:
            return variant, "named"
    return " / ".join(part.upper() if part in _SIZE_WORDS else part.title() for part in want), (
        "unknown"
    )
