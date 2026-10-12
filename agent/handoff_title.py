"""A real title and goal for the runtime hand-off card, with no model call.

When the router backstop hands a turn to a helper, the card used to show two
fixed placeholders ("taking over the rest of your request" -> "the finished
result").  This builds both from what is already in hand:

* the title (``work``): a short gerund phrase from the user's message,
  ``compare 3 options for adding a second GPU ...`` -> ``comparing 3 options for
  adding a second GPU`` (at most ``TITLE_CHARS``, cut on a word);
* the goal (``outcome``): the router's own ``reason`` when it describes the job
  (a "Compare three GPUs against ..." sentence), otherwise a short noun phrase
  from the message (``3 options for adding a second GPU, compared``), at most
  ``GOAL_CHARS``.

The raw message is never pasted: fenced code, quoted blocks, links and
everything past the first sentence are dropped before anything is shown, and
both strings are length-capped.  Pure functions; nothing here can raise.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

TITLE_CHARS = 50
GOAL_CHARS = 60
FALLBACK_TITLE = "taking over the rest of your request"
FALLBACK_GOAL = "the finished result"

# verb -> (gerund, past participle)
_VERBS = {
    "add": ("adding", "added"), "analyse": ("analysing", "analysed"),
    "analyze": ("analyzing", "analyzed"), "audit": ("auditing", "audited"),
    "build": ("building", "built"), "check": ("checking", "checked"),
    "clean": ("cleaning", "cleaned"), "compare": ("comparing", "compared"),
    "convert": ("converting", "converted"), "create": ("creating", "created"),
    "debug": ("debugging", "debugged"), "deploy": ("deploying", "deployed"),
    "design": ("designing", "designed"), "diagnose": ("diagnosing", "diagnosed"),
    "do": ("doing", "done"), "download": ("downloading", "downloaded"),
    "draft": ("drafting", "drafted"), "edit": ("editing", "edited"),
    "email": ("emailing", "emailed"), "export": ("exporting", "exported"),
    "figure": ("figuring", "figured"), "find": ("finding", "found"),
    "fix": ("fixing", "fixed"), "gather": ("gathering", "gathered"),
    "generate": ("generating", "generated"), "get": ("getting", "got"),
    "install": ("installing", "installed"), "investigate": ("investigating", "investigated"),
    "list": ("listing", "listed"), "look": ("looking", "looked"),
    "make": ("making", "made"), "migrate": ("migrating", "migrated"),
    "optimise": ("optimising", "optimised"), "optimize": ("optimizing", "optimized"),
    "package": ("packaging", "packaged"), "plan": ("planning", "planned"),
    "prepare": ("preparing", "prepared"), "put": ("putting", "put"),
    "read": ("reading", "read"), "rebuild": ("rebuilding", "rebuilt"),
    "refactor": ("refactoring", "refactored"), "release": ("releasing", "released"),
    "research": ("researching", "researched"), "review": ("reviewing", "reviewed"),
    "run": ("running", "run"), "scan": ("scanning", "scanned"),
    "search": ("searching", "searched"), "send": ("sending", "sent"),
    "set": ("setting", "set"), "ship": ("shipping", "shipped"),
    "summarise": ("summarising", "summarised"), "summarize": ("summarizing", "summarized"),
    "test": ("testing", "tested"), "track": ("tracking", "tracked"),
    "transcribe": ("transcribing", "transcribed"), "update": ("updating", "updated"),
    "upgrade": ("upgrading", "upgraded"), "verify": ("verifying", "verified"),
    "write": ("writing", "written"),
}

# Opening filler, stripped repeatedly from the front (case-insensitive).
_FILLER = re.compile(
    r"^(?:"
    r"hey+|hi|hello|ok(?:ay)?|so|well|alright|right|also|and|then|now|and then|"
    r"(?:can|could|would|will) you(?: please| also| just| maybe)*|"
    r"please|pls|kindly|just|quickly|go ahead and|i(?:'d| would)? (?:need|want|like) you to|"
    r"i need|i want|i'd like|i would like|let'?s|lets|"
    r"(?:use|spawn|launch|start|run|send|get|have|give me|using)\s+(?:\d+|one|two|three|four|five|six|a few|several|some|multiple|many|more|parallel)?\s*"
    r"(?:parallel |extra |more |separate |background )*(?:sub-?agents?|helpers?|workers?|agents?)"
    r"(?: at once| in parallel| at the same time| together)?(?:\s+(?:to|and|that|which|so|who|for))?|"
    r"in the background|in parallel|"
    r"(?:one|two|three|four|five|\d+)\s+(?:sub-?agents?|helpers?)\s+(?:to|for)|"
    r"to"
    r")\b[\s,:;.\-–—]*",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_QUOTE_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
_QUOTED = re.compile(r"[\"“‘«„]([^\"”’»]{25,})[\"”’»]")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_PAREN = re.compile(r"\([^()]{0,200}\)|\[[^\[\]]{0,200}\]")
_TRAIL = {
    "a", "an", "the", "of", "for", "to", "and", "or", "in", "on", "at", "with", "by",
    "from", "into", "about", "that", "which", "my", "your", "our", "their", "its",
    "as", "than", "then", "but", "so", "is", "are", "be", "per", "via", "using",
}
# Reasons that describe the sizing, not the job; never shown as the goal.
_META = re.compile(
    r"\b(?:requires?|needs?|is a|are a|quick|simple|small|brief|bounded|long[- ]running|"
    r"several|multiple|many|lookups?|tool calls?|helpers?|sub-?agents?|direct(?:ly)?|"
    r"sized?|short task|one[- ]shot|trivial|single task|multi-?part|big job|large job|"
    r"user asked|you asked)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def _scrub(message: str) -> str:
    """The message without code fences, quote lines, long quoted spans, links."""
    text = str(message or "")
    text = _FENCE.sub(" ", text)
    text = _QUOTE_LINE.sub(" ", text)
    text = _QUOTED.sub(" ", text)
    text = _URL.sub(" ", text)
    return text


def _first_clause(text: str) -> str:
    """First sentence/line that has words in it."""
    for piece in _SENTENCE_END.split(text):
        piece = _flat(piece)
        if sum(ch.isalpha() for ch in piece) >= 3:
            return piece
    return ""


def _cut(text: str, limit: int) -> str:
    """At most ``limit`` chars, cut on a word, no dangling connector."""
    text = _flat(text).strip(" ,;:-–—")
    if len(text) <= limit:
        return text.rstrip(" ,;:-–—.")
    cut = text[:limit]
    if text[limit] != " ":
        space = cut.rfind(" ")
        if space <= 0:
            return ""  # one unbroken token (a path, a hash): nothing readable to show
        cut = cut[:space]
    words = cut.split()
    while len(words) > 1 and words[-1].lower().strip(",;:") in _TRAIL:
        words.pop()
    return " ".join(words).rstrip(" ,;:-–—.")


_CLAUSE_SPLIT = re.compile(
    r",\s+|\s+(?:and then|then|after that|afterwards|before that|once that'?s done)\s+", re.IGNORECASE)
_AND_SPLIT = re.compile(r"\s+and\s+", re.IGNORECASE)
_VAGUE = frozenset({"this", "that", "it", "these", "those", "them", "something", "stuff", "things", "everything"})
_QUESTION = re.compile(r"^(?:why|how|what|whats|what's|where|when|who|is|are|does|do|did|can|could|should|which)\b", re.I)


def _head_clause(text: str, limit: int) -> str:
    """The first step of a multi-step sentence; the title names one job."""
    head = _CLAUSE_SPLIT.split(text, maxsplit=1)[0]
    if len(head) > limit:
        parts = _AND_SPLIT.split(head, maxsplit=1)
        if len(parts[0].split()) >= 3:
            head = parts[0]
    return head


def _strip_filler(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _FILLER.sub("", text, count=1).lstrip()
    return text


def _action(message: str) -> Tuple[str, str]:
    """``(verb, rest)`` of the message's first job clause; verb may be ``""``."""
    text = _first_clause(_scrub(message))
    text = _strip_filler(text)
    # Cut an explanatory tail ("... because", "... so that") and parentheses.
    text = _PAREN.sub(" ", text)
    text = re.split(r"\s+(?:because|so that|since|which means)\s+|\s*[:;]\s+|\s+[-–—]\s+", text, maxsplit=1)[0]
    text = _flat(text)
    first, _, rest = text.partition(" ")
    verb = first.lower().strip(",.")
    if verb in _VERBS:
        return verb, rest.strip()
    return "", text


def _lead_lower(text: str) -> str:
    """Lowercase the first letter only (keeps GPU, Gatineau, USB-C)."""
    if len(text) > 1 and text[1].isupper():
        return text
    return text[:1].lower() + text[1:]


def _job_like_reason(reason: str) -> str:
    """The router's reason when it names the job; ``""`` when it explains the sizing."""
    text = _first_clause(str(reason or ""))
    text = _flat(text).rstrip(".!? ")
    if len(text) < 12 or _META.search(text):
        return ""
    first = text.split(" ", 1)[0].lower().strip(",")
    if first not in _VERBS and not first.endswith("ing"):
        return ""
    text = re.sub(r"\b(?:the )?user['’]s\b", "your", text, flags=re.IGNORECASE)
    text = re.sub(r"\bthe user\b", "you", text, flags=re.IGNORECASE)
    return text


def title_and_goal(message: str, reason: str = "", fallback_goal: str = "") -> Tuple[str, str]:
    """``(title, goal)`` for the hand-off card.  Never raises, never empty."""
    try:
        verb, rest = _action(message)
        rest = _flat(rest).rstrip(" ?!.,;:")
        if rest.lower() in _VAGUE:
            rest = ""
        if not rest:  # "do it", "ok": the message names nothing; the plan's own task may
            verb, rest = _action(fallback_goal) if fallback_goal else ("", "")
            rest = _flat(rest).rstrip(" ?!.,;:")
        title = noun = ""
        if verb and rest:
            gerund, participle = _VERBS[verb]
            title = _cut(f"{gerund} {_head_clause(rest, TITLE_CHARS)}", TITLE_CHARS)
            phrase = _head_clause(rest, GOAL_CHARS)
            noun = _cut(f"{phrase}, {participle}", GOAL_CHARS)
            if f", {participle}" not in noun:  # no room for the participle: keep the phrase whole
                noun = _cut(phrase, GOAL_CHARS)
        elif rest and _QUESTION.match(rest):
            body = _head_clause(rest, TITLE_CHARS)
            title = _cut("answering: " + _lead_lower(body), TITLE_CHARS)
            noun = _cut(rest, GOAL_CHARS)
        elif rest:
            title = _cut("working on " + _lead_lower(_head_clause(rest, TITLE_CHARS)), TITLE_CHARS)
            noun = _cut(rest, GOAL_CHARS)
        if len(title.split()) < 3:
            title = FALLBACK_TITLE
        goal = _job_goal(reason) or noun or FALLBACK_GOAL
        return title, goal
    except Exception:
        return FALLBACK_TITLE, FALLBACK_GOAL


def _job_goal(reason: str) -> str:
    """The router's reason as the goal: whole when it fits, else its first clause."""
    text = _job_like_reason(reason)
    if not text:
        return ""
    if len(text) > GOAL_CHARS:
        head = re.split(r",\s+|;\s+|\s+(?:and|then|while|with)\s+", text, maxsplit=1)[0]
        text = head if 15 <= len(head) <= GOAL_CHARS else ""
    return _cut(text, GOAL_CHARS) if text else ""


def handoff_summary(message: str, reason: str = "", fallback_goal: str = "") -> Optional[dict]:
    """``{"work": title, "outcome": goal}`` for ``delegate_task(handoff=...)``."""
    title, goal = title_and_goal(message, reason, fallback_goal)
    return {"work": title, "outcome": goal}
