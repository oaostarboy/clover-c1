"""A real title and goal for the runtime hand-off card.

The title names the WORK, never the user's latest message.  The best source is
one small-model call (``agent.handoff_notes.handoff_brief``; its answer is
checked here by ``clean_model_title`` / ``clean_model_goal``).  This module is
the fallback that needs no model:

* the title (``work``): a short gerund phrase from the user's message when it
  is a task, ``compare 3 options for adding a second GPU ...`` -> ``comparing 3
  options for adding a second GPU`` (at most ``TITLE_CHARS``, cut on a word);
* a message that is NOT a task (a question, a complaint, chit-chat, anything
  with profanity) is never turned into a title.  The title then comes, in
  order, from the in-progress todo item, the plan's own task, the last
  hand-off's title in this session, the earlier request this turn continues,
  else ``finishing this up``;
* the goal (``outcome``): the router's own ``reason`` when it describes the job
  (a "Compare three GPUs against ..." sentence), otherwise a short phrase from
  the task that reads on its own: a noun where one is natural
  (``comparison of 3 options for adding a second GPU``), a noun phrase with the
  verb dropped (``run a diagnostic on why that fuse tripped`` ->
  ``diagnostic on why that fuse tripped``), else the cleaned request itself
  (``send the AutoHDR skill``).  The verb is never moved to the end.  At most
  ``GOAL_CHARS``.

Profanity is always stripped.  The raw message is never pasted: fenced code,
quoted blocks, links and everything past the first sentence are dropped before
anything is shown, and both strings are length-capped.  Pure functions;
nothing here can raise.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence, Tuple

TITLE_CHARS = 50
GOAL_CHARS = 60
FALLBACK_TITLE = "finishing this up"
FALLBACK_GOAL = "the finished result"

# verb -> (gerund, past participle)
_VERBS = {
    "add": ("adding", "added"), "analyse": ("analysing", "analysed"),
    "analyze": ("analyzing", "analyzed"), "audit": ("auditing", "audited"),
    "build": ("building", "built"), "check": ("checking", "checked"),
    "clean": ("cleaning", "cleaned"), "compare": ("comparing", "compared"),
    "convert": ("converting", "converted"), "count": ("counting", "counted"),
    "create": ("creating", "created"),
    "debug": ("debugging", "debugged"), "deploy": ("deploying", "deployed"),
    "design": ("designing", "designed"), "diagnose": ("diagnosing", "diagnosed"),
    "do": ("doing", "done"), "download": ("downloading", "downloaded"),
    "draft": ("drafting", "drafted"), "edit": ("editing", "edited"),
    "email": ("emailing", "emailed"), "export": ("exporting", "exported"),
    "figure": ("figuring", "figured"), "find": ("finding", "found"),
    "fix": ("fixing", "fixed"), "gather": ("gathering", "gathered"),
    "generate": ("generating", "generated"), "get": ("getting", "got"),
    "implement": ("implementing", "implemented"),
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
    "continue": ("continuing", "continued"), "finish": ("finishing", "finished"),
    "resume": ("resuming", "resumed"), "rename": ("renaming", "renamed"),
    "move": ("moving", "moved"), "remove": ("removing", "removed"),
    "delete": ("deleting", "deleted"), "show": ("showing", "shown"),
    "explain": ("explaining", "explained"), "open": ("opening", "opened"),
    "push": ("pushing", "pushed"), "merge": ("merging", "merged"),
    "commit": ("committing", "committed"), "schedule": ("scheduling", "scheduled"),
    "book": ("booking", "booked"), "buy": ("buying", "bought"),
    "publish": ("publishing", "published"), "reply": ("replying", "replied"),
    "restart": ("restarting", "restarted"), "start": ("starting", "started"),
    "wire": ("wiring", "wired"),
    "configure": ("configuring", "configured"), "implement": ("implementing", "implemented"),
    "improve": ("improving", "improved"), "finalize": ("finalizing", "finalized"),
    "finalise": ("finalising", "finalised"), "complete": ("completing", "completed"),
}

# verb -> the noun that heads the same job ("compare X" -> "comparison of X")
_NOUNS = {
    "analyse": "analysis of", "analyze": "analysis of", "audit": "audit of",
    "compare": "comparison of", "diagnose": "diagnosis of", "plan": "plan for",
    "research": "research on", "review": "review of", "summarise": "summary of",
    "summarize": "summary of", "investigate": "investigation of",
    "design": "design of", "migrate": "migration of", "transcribe": "transcript of",
}
# Verbs that only carry the noun after them: "run a diagnostic" -> "diagnostic".
_LIGHT_VERBS = frozenset({"run", "do", "make", "perform", "give", "write", "put", "set"})
_LEADING_ARTICLE = re.compile(r"^(?:a|an|the|some|one)\s+", re.IGNORECASE)

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


# Words that are never shown on a card, whoever typed them.
_PROFANITY = re.compile(
    r"\b(?:f+u+c+k\w*|fck\w*|fuk\w*|motherf\w*|sh[i1]+t\w*|bullsh\w*|damn\w*|goddamn\w*|"
    r"bitch\w*|bastard\w*|asshole\w*|arse(?:hole)?s?|ass|crap\w*|dick(?:head)?s?|piss(?:ed|ing)?|cunt\w*|wtf|stfu)\b",
    re.IGNORECASE,
)
# A question or chit-chat opener: never a task.  The auxiliaries (do, is...)
# only count when a pronoun follows ("do you ...") because "do a full
# diagnostic" is an order.  "can/could/would you <action verb>" is a polite
# order and is left to the verb check ("can you not stop" has no verb).
_WH = re.compile(r"^(?:why|how|what|whats|what's|where|when|who|which|whose)\b", re.IGNORECASE)
_AUX = re.compile(
    r"^(?:is|are|was|were|does|do|did|should|have|has|had|am)\s+"
    r"(?:you|i|we|they|he|she|it|this|that|there|these|those|my|your|the)\b", re.IGNORECASE)
_NEGATED = re.compile(r"^(?:not|never|no)\b", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


def strip_profanity(text: str) -> str:
    """``text`` without profane words (and without the gaps they leave)."""
    out = _PROFANITY.sub(" ", str(text or ""))
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,;:])\s*(?=[,.;:!?])", "", out)
    return _SPACES.sub(" ", out).strip(" ,;:-")


def has_profanity(text: str) -> bool:
    return bool(_PROFANITY.search(str(text or "")))


def _is_question(text: str) -> bool:
    text = _flat(text)
    return bool(text.endswith("?") or _WH.match(text) or _AUX.match(text))


def _task_parts(text: str, *, noun_ok: bool = False) -> Tuple[str, str]:
    """``(title, goal noun)`` of ``text`` when it is an order to do something.

    ``("", "")`` when it is not: a question, a complaint, chit-chat, anything
    profane, an order about nothing in particular ("push it to the domain"), or
    a sentence that does not start with a known action verb.  With ``noun_ok``
    (a todo item, not a chat message) a plain noun phrase becomes ``working on
    <phrase>``.  Never raises."""
    try:
        raw = _scrub(text)
        if not _flat(raw) or has_profanity(raw):
            return "", ""
        first = _first_clause(raw)
        if _WH.match(first) or _AUX.match(first):
            return "", ""
        verb, rest = _action(raw)
        rest = _flat(rest).rstrip(" ?!.,;:")
        lead = rest.split(" ", 1)[0].lower() if rest else ""
        if not rest or lead in _VAGUE or _NEGATED.match(rest):
            return "", ""
        if verb:
            head = _head_clause(rest, TITLE_CHARS)
            title = _cut(f"{_VERBS[verb][0]} {head}", TITLE_CHARS)
            noun = _cut(_goal_phrase(verb, _head_clause(rest, GOAL_CHARS)), GOAL_CHARS)
            return (title, noun) if len(title.split()) >= 3 else ("", "")
        if noun_ok and not _is_question(first):
            body = _lead_lower(_head_clause(rest, TITLE_CHARS))
            title = _cut("working on " + body, TITLE_CHARS)
            return (title, _cut(rest, GOAL_CHARS)) if len(title.split()) >= 3 else ("", "")
    except Exception:
        pass
    return "", ""


def _is_default(title: str) -> bool:
    return _flat(title).lower().rstrip(".") in ("", FALLBACK_TITLE)


def _clean_title(text: str) -> str:
    """A title carried over from an earlier hand-off: no profanity, no
    ``answering:`` echo, capped; ``""`` when nothing usable is left."""
    text = _flat(text)
    if re.match(r"^(?:answering|replying to|responding to)\b", text, re.IGNORECASE):
        return ""
    text = _cut(strip_profanity(text), TITLE_CHARS)
    return "" if len(text.split()) < 3 or _is_default(text) else text


def clean_model_title(text: str, message: str = "") -> str:
    """The model's title if it is usable, else ``""``.

    Usable: 3 to 8 words, a gerund or ``working on`` phrase, no profanity, no
    ``answering:`` prefix, no question, and not a quote of the user's latest
    message."""
    try:
        title = _flat(text).strip("\"'`“”‘’ .")
        if not title or has_profanity(title) or _is_question(title):
            return ""
        if re.match(r"^(?:answering|replying to|responding to)\b", title, re.IGNORECASE):
            return ""
        title = _cut(_lead_lower(title), TITLE_CHARS)
        words = title.split()
        if not 3 <= len(words) <= 8 or not words[0].lower().endswith("ing"):
            return ""
        said = re.sub(r"[^a-z0-9 ]", "", _flat(message).lower())
        mine = re.sub(r"[^a-z0-9 ]", "", title.lower())
        if said and mine and (mine in said or said in mine):
            return ""
        return title
    except Exception:
        return ""


def clean_model_goal(text: str, message: str = "") -> str:
    """The model's goal if usable (at most 8 words, no profanity), else ``""``."""
    try:
        goal = _flat(text).strip("\"'`“”‘’ ").rstrip(" .!?")
        if not goal or has_profanity(goal) or len(goal.split()) > 12:
            return ""
        said = re.sub(r"[^a-z0-9 ]", "", _flat(message).lower())
        mine = re.sub(r"[^a-z0-9 ]", "", goal.lower())
        if said and len(mine) > 8 and (mine in said or said in mine):
            return ""
        return _cut(goal, GOAL_CHARS)
    except Exception:
        return ""


class HandoffText(tuple):
    """``(title, goal)`` that also says where the title came from (``source``:
    ``message`` | ``todo`` | ``plan`` | ``delegation`` | ``earlier`` | ``default``)."""

    def __new__(cls, title: str, goal: str, source: str):
        obj = super().__new__(cls, (title, goal))
        obj.source = source
        return obj


def resolve(
    message: str, reason: str = "", fallback_goal: str = "", *,
    todo: str = "", last_title: str = "", earlier: Sequence[str] = (),
) -> HandoffText:
    """Title and goal for the card, always from the WORK.

    The message gives the title only when it is an order; otherwise (a
    complaint, a question, chit-chat, profanity) the title comes from, in
    order: the in-progress ``todo`` item, the plan's own task
    (``fallback_goal``), the title of the last hand-off in this session, the
    earlier request(s) this turn continues, else ``finishing this up``.
    Never raises, never empty."""
    try:
        title, noun = _task_parts(message)
        source = "message"
        if not title:
            title, noun, source = "", "", "default"
            for src, text in (("todo", todo), ("plan", fallback_goal)):
                t, n = _task_parts(text, noun_ok=(src == "todo"))
                if t:
                    title, noun, source = t, n, src
                    break
            if not title:
                prior = _clean_title(last_title)
                if prior:
                    title, source = prior, "delegation"
            if not title:
                for text in earlier or ():
                    t, n = _task_parts(text)
                    if t:
                        title, noun, source = t, n, "earlier"
                        break
        if not title:
            title, source = FALLBACK_TITLE, "default"
        goal = _job_goal(reason) or noun or FALLBACK_GOAL
        return HandoffText(strip_profanity(title) or FALLBACK_TITLE, strip_profanity(goal) or FALLBACK_GOAL, source)
    except Exception:
        return HandoffText(FALLBACK_TITLE, FALLBACK_GOAL, "default")


def title_and_goal(message: str, reason: str = "", fallback_goal: str = "", **context) -> Tuple[str, str]:
    """``(title, goal)`` for the hand-off card.  Never raises, never empty."""
    found = resolve(message, reason, fallback_goal, **context)
    return found[0], found[1]


# ── distinct titles within one chat ────────────────────────────────────────

RECENT_WINDOW_SECONDS = 30 * 60
_TITLE_KEY = re.compile(r"[^a-z0-9 ]")
_LEAD_GERUND = re.compile(r"^(?:[a-z]+ing)\s+(?:on\s+)?", re.IGNORECASE)
# Words a variant is built from; a title that starts with one is already one.
_VARIANT_LEADS = (
    "checking", "finishing the checks on", "wrapping up", "following up on",
    "double-checking", "continuing",
)
# Completion turn: this helper checks and finishes what the earlier one started.
_COMPLETION_LADDER = (
    "checking {obj}", "finishing the checks on {obj}", "wrapping up {obj}",
    "double-checking {obj}",
)
_COMPLETION_LADDER_BARE = (
    "checking the helper results", "finishing the helper checks",
    "wrapping up the helper results", "double-checking the helper results",
)
# Human turn: another helper on the same chat's work.
_FOLLOW_LADDER = (
    "following up on {obj}", "continuing {obj}", "taking another pass at {obj}",
)
_FOLLOW_LADDER_BARE = (
    "following up on the earlier work", "continuing the earlier work",
    "taking another pass at the earlier work",
)


def title_key(title: str) -> str:
    """A title as compared for sameness: lowercase words only."""
    return _flat(_TITLE_KEY.sub(" ", str(title or "").lower()))


def title_object(title: str) -> str:
    """What a title is about (``restoring the Clover gateway`` -> ``the Clover
    gateway``); ``""`` when nothing readable is left."""
    text = _flat(title).strip(" .")
    if text.lower().startswith(_VARIANT_LEADS):
        return ""  # a variant of a variant: no clean object to reuse
    body = _LEAD_GERUND.sub("", text, count=1).strip()
    if body == text or len(body.split()) < 2:
        return ""
    return _cut(body, TITLE_CHARS - 22)


def is_recent_title(title: str, recent: Sequence[str]) -> bool:
    key = title_key(title)
    return bool(key) and any(key == title_key(r) for r in recent or ())


def distinct_title(title: str, recent: Sequence[str], *, completion: bool = False) -> str:
    """``title`` unless this chat already had a hand-off with that title in the
    last half hour; then a variant that says what THIS helper does.

    No model needed: a completion turn's helper checks and finishes what the
    earlier one produced (``checking the Clover gateway``); a later helper on
    a human turn follows up on it.  The wording comes from the earlier title's
    own subject, never repeats any recent title, and never raises."""
    try:
        recent = [r for r in (recent or ()) if _flat(r)]
        if not is_recent_title(title, recent):
            return title
        obj = title_object(title) or next((o for o in map(title_object, recent) if o), "")
        ladder = (
            (_COMPLETION_LADDER if obj else _COMPLETION_LADDER_BARE) if completion
            else (_FOLLOW_LADDER if obj else _FOLLOW_LADDER_BARE)
        )
        for pattern in ladder:
            candidate = _cut(pattern.format(obj=obj), TITLE_CHARS)
            if candidate and not is_recent_title(candidate, recent):
                return candidate
        n = 2
        while True:  # every rung used: number it, still distinct and readable
            candidate = _cut(f"{ladder[0].format(obj=obj)} ({n})", TITLE_CHARS + 4)
            if not is_recent_title(candidate, recent):
                return candidate
            n += 1
    except Exception:
        return title


def stage_handoff(goals: Sequence[str], earlier: Sequence[str] = ()) -> dict:
    """``{"work", "outcome"}`` for one stage of a router plan, from THAT
    stage's own task(s) -- never from the user's whole request or the plan's
    overall reason, which would put the same title and the whole plan's goal on
    every stage.  One task: its short gerund title and a noun for its result.
    Several: their titles joined (``N subagents`` when too long) and their
    results joined."""
    goals = [g for g in (goals or ()) if _flat(g)]
    if not goals:
        return {"work": FALLBACK_TITLE, "outcome": FALLBACK_GOAL}
    titles: list = []
    nouns: list = []
    for g in goals:
        found = resolve(g, "", "", earlier=earlier)
        titles.append(found[0] if found.source == "message" else task_title(g))
        nouns.append(found[1] if found.source == "message" else "")
    if len(goals) == 1:
        work = titles[0]
    else:
        joined = " and ".join(titles[:2]) if len(goals) == 2 else ""
        work = joined if joined and len(joined) <= TITLE_CHARS else f"{len(goals)} subagents"
    outcome = " + ".join(n for n in nouns if n)
    outcome = _cut(outcome, GOAL_CHARS) if outcome else ""
    return {"work": strip_profanity(work) or FALLBACK_TITLE,
            "outcome": strip_profanity(outcome) or FALLBACK_GOAL}


def _goal_phrase(verb: str, phrase: str) -> str:
    """The job as a phrase that reads on its own; never ``<phrase>, <verb>``."""
    phrase = _flat(phrase)
    if verb in _NOUNS:
        return f"{_NOUNS[verb]} {phrase}"
    if verb in _LIGHT_VERBS:
        bare = _LEADING_ARTICLE.sub("", phrase)
        if bare and bare != phrase:
            return bare
    return f"{verb} {phrase}"


def _job_goal(reason: str) -> str:
    """The router's reason as the goal: whole when it fits, else its first clause."""
    text = _job_like_reason(reason)
    if not text:
        return ""
    if len(text) > GOAL_CHARS:
        head = re.split(r",\s+|;\s+|\s+(?:and|then|while|with)\s+", text, maxsplit=1)[0]
        text = head if 15 <= len(head) <= GOAL_CHARS else ""
    return _cut(text, GOAL_CHARS) if text else ""


def handoff_summary(message: str, reason: str = "", fallback_goal: str = "", **context) -> Optional[dict]:
    """``{"work": title, "outcome": goal}`` for ``delegate_task(handoff=...)``."""
    title, goal = title_and_goal(message, reason, fallback_goal, **context)
    return {"work": title, "outcome": goal}


# A task brief often opens with a role line ("You are building the site.") or
# "Your task is to ..."; neither names the work.
_ROLE_LINE = re.compile(r"^(?:you(?:'re| are| will be| must)\b|your (?:task|job|goal|mission) is\b)", re.IGNORECASE)
_DANGLING = re.compile(r"\s+(?:at|on|from|in|of|to|see)(?=\s+(?:and|or|then)\b)", re.IGNORECASE)
_TASK_PREFIX = re.compile(r"^your (?:task|job|goal|mission) is(?: to)?\s+", re.IGNORECASE)


def task_title(goal: str, explicit: str = "") -> str:
    """A short card title for one delegated task, with no model call.

    The task's own ``title`` when it has one; otherwise a gerund phrase from its
    goal (``implement the contact form`` -> ``implementing the contact form``).
    Never empty, never raises.
    """
    try:
        own = _flat(str(explicit or "").replace("*", ""))
        if own:
            return _cut(own, TITLE_CHARS)
        scrubbed = _DANGLING.sub(" ", _scrub(goal))  # "review the PR at <link> and ..." -> "... PR and ..."
        pieces = [_flat(p) for p in _SENTENCE_END.split(scrubbed) if _flat(p)]
        text = ""
        for i, piece in enumerate(pieces):
            if _ROLE_LINE.match(piece) and not _TASK_PREFIX.match(piece):
                continue
            text = _TASK_PREFIX.sub("", piece)
            break
        text = text or (pieces[0] if pieces else "")
        title = title_and_goal(text)[0]
        if title == FALLBACK_TITLE:
            title = _cut(_head_clause(_flat(text), TITLE_CHARS), TITLE_CHARS) or "background task"
        return title
    except Exception:
        return "background task"


def dispatch_work_title(goals, titles=()) -> str:
    """The card title of a dispatch that came without a ``handoff`` summary:
    the one task's title, or ``N subagents``."""
    goals = [g for g in (goals or ()) if g]
    if len(goals) == 1:
        return task_title(goals[0], (list(titles or ()) or [""])[0])
    return f"{len(goals)} subagents"
