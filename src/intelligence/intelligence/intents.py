"""Questions that are about something Neo can *see*, not something it knows.

"What is this?" cannot be answered from the campus dataset or by the language
model, because neither has the frame. It needs one pass of the object model
over one live frame -- which is expensive enough that it runs on demand only
(CLAUDE.md), so something has to decide when a question is that kind of
question.

Deliberately a phrase matcher rather than a model or a keyword bag:

* **A keyword bag is wrong here.** "what" and "this" are the two commonest
  words in the questions this robot gets. "What time is this class?" and "What
  block is this room in?" are campus questions that a bag containing
  {what, this} would route to the camera, and the camera would confidently say
  "laptop".
* **A false positive is worse than a false negative.** Missing the intent means
  the language model answers "I cannot see what you are holding", and the
  person rephrases. Hitting it wrongly means a second network runs and Neo
  interrupts a directions question to name an object nobody asked about.

So the rule is: match the whole shape of the phrase, and only where the object
is genuinely unnamed -- a bare demonstrative with no noun after it.
"""

from __future__ import annotations

import re

_PATTERNS = (
    # "what is this/that", "what's this thing", "what is this object"
    r"what(?:'s| is| are)?\s+(?:this|that|these|those)(?:\s+(?:thing|object|item|one))?\s*\??$",
    # "what am I holding", "what is he holding up"
    r"what\s+(?:am\s+i|are\s+you|is\s+(?:he|she|this\s+person))\s+holding(?:\s+up)?\s*\??$",
    # "what do you see", "what can you see"
    r"what\s+(?:do|can)\s+you\s+see\s*\??$",
    # "identify this", "recognise that object", "name this"
    r"(?:identify|recognis|recogniz|name)\w*\s+(?:this|that|the)\s*(?:thing|object|item)?\s*\??$",
    # "do you know what this is", "can you tell me what this is"
    r"(?:do you know|can you tell me|tell me)\s+what\s+(?:this|that)\s+is\s*\??$",
    # "look at this", "have a look at this"
    r"(?:take a |have a )?look at (?:this|that)(?:\s+(?:thing|object))?\s*\??$",
)

_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in _PATTERNS)

_LEADING_FILLER = re.compile(
    r"^(?:hey|hi|hello|ok|okay|so|um|uh|please|excuse me|sorry)\b[\s,]*", re.IGNORECASE
)


def _normalise(text: str) -> str:
    cleaned = text.strip().lower()
    # Strip a run of openers, not just one: "hey, ok so what is this" happens.
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = _LEADING_FILLER.sub("", cleaned).strip()
    # Collapse whitespace so a transcript with odd spacing still matches.
    return re.sub(r"\s+", " ", cleaned).strip(" ,.")


def wants_object_identification(text: str) -> bool:
    """Is this asking about a thing in front of the camera?"""
    if not text:
        return False
    cleaned = _normalise(text)
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in _COMPILED)


def describe_object(label: str) -> str:
    """Phrase an identification as an answer.

    Hedged on purpose. The object model is a general COCO detector being asked
    about whatever someone held up, and stating a guess as a fact is how a
    receptionist robot confidently calls a lunchbox a laptop.
    """
    if not label:
        return "I can see something, but I am not sure what it is."
    article = "an" if label[:1].lower() in "aeiou" else "a"
    return f"That looks like {article} {label}."


def no_object_seen() -> str:
    return "I cannot see anything clearly enough to identify. Try holding it up to my camera."
