"""Pick a small fast model for latency-critical auxiliary tasks.

The delegation router and title generation are tiny side calls (a JSON verdict,
an 8-token title).  Run on a frontier reasoning model they cost seconds and
tokens for nothing, so ``auto`` resolves them to a small fast model unless the
user pinned one (``auxiliary.<task>.provider/model``) or turned the preference
off (``auxiliary.<task>.prefer_fast_model: false``).

Prior art this follows: OpenCode's ``small_model`` (explicit override, a
per-provider priority search, fall back to the full model, no reasoning on the
small call); Claude Code's hard-coded Haiku for background work (users asked
for an override, so we keep one); maslul's "cheap classifier behind a free
deterministic bypass"; OpenSmartRoute's "price the live catalog".

This module holds the pure parts: the rung table, the catalog / price pickers,
the speed probe with its 7-day on-disk cache, and the small-talk allow-list.
``agent.auxiliary_client`` wires them into client resolution.

Resolution ladder for the MAIN provider (first hit wins), ``how`` in brackets:

1. the provider profile's explicit ``fast_aux_model``          [profile]
2. a family match in the live catalog (``FAST_RUNGS``)         [family]
3. the curated ``default_aux_model`` when it is itself a fast
   family, or the provider's own live hook                     [profile]
4. the cheapest chat-capable model, when the catalog has prices [price]
5. a cached speed-probe winner                                  [probe]
6. a fast model of another provider the user already has set
   up as a fallback, whose credentials resolve unprompted        [borrowed]
7. the main model, with a longer router timeout                 [main]

Step 5 is filled in by a background thread the first time (never inline on a
user turn); until it lands the main model is used.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── rungs ──────────────────────────────────────────────────────────────────

# Rolling "-latest" aliases first: the only ids that cannot rot.  Then the
# family rungs in measured order (small/fast first).  The old specific ids stay
# at the bottom as fallbacks for catalogs that carry only a pinned id.
FAST_RUNGS: Tuple[str, ...] = (
    "gpt-mini-latest",
    "gpt-nano-latest",
    "claude-haiku-latest",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "-luna",
    "flash-lite",
    "-flash",
    "haiku",
    "-nano",
    "-lite",
    "-mini",
    "-turbo",
    "-small",
    # xAI: "grok-4.20-0309-non-reasoning" answers a router prompt in ~1.0 s on
    # the xAI OAuth route vs ~5.8 s for grok-4.6 (measured); the thinking
    # sibling is excluded below.
    "non-reasoning",
    "-fast",
    # legacy specific rungs, kept as fallbacks
    "gpt-5.4-nano",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "haiku-4.5",
    "gemini-3.6-flash",
)

# Substrings that disqualify an otherwise matching id (see the notes in
# agent.auxiliary_client): thinkers, queues, encoders, and the speech / image /
# search siblings a provider names after a chat model ("gpt-4o-mini-tts").
FAST_EXCLUDE: Tuple[str, ...] = (
    "thinking", "reason", "-r1", "minilm", ":batch", ":free",
    "o1-", "o3-", "o4-", "codex", "audio", "-vl", "embed",
    "tts", "transcribe", "realtime", "image", "imagine", "video", "search",
    "moderation", "multi-agent",
    # "-pro" is the heavier sibling of a small model ("gpt-5.6-luna-pro").
    "-pro",
)

# "grok-4-fast-non-reasoning" is the opposite of a thinker; without this the
# "reason" exclusion would throw out the one variant we want.
_NON_THINKING = re.compile(r"non[-_ ]?(?:reasoning|thinking)")

# Providers that have no ProviderProfile of their own (``provider: openai`` is
# an alias for a custom endpoint on api.openai.com) but a known small tier.
STATIC_FAST_MODELS: Dict[str, str] = {
    "openai": "gpt-6-luna",
}

@dataclass(frozen=True)
class FastPick:
    """The route chosen for a latency-critical auxiliary task."""

    provider: str
    model: str
    how: str  # profile | family | price | probe | borrowed | main

    @property
    def is_main(self) -> bool:
        return self.how == "main"


PROBE_MAX_CANDIDATES = 3
PROBE_TIMEOUT_SECONDS = 6.0
PROBE_MAX_TOKENS = 40
CACHE_TTL_SECONDS = 7 * 24 * 3600
NEGATIVE_TTL_SECONDS = 24 * 3600
# A "main" result is re-checked this often so a landed probe takes over.
MAIN_RECHECK_SECONDS = 60.0

_VERSION_CHUNK_RE = re.compile(r"(\d+(?:\.\d+)?)")

PROBE_MESSAGES: List[Dict[str, str]] = [
    {"role": "system", "content": 'Reply with one JSON object only: {"mode": "direct"}'},
    {"role": "user", "content": "Size this request: list the files in my downloads folder."},
]


def recency_key(model_id: str) -> tuple:
    """Sort key putting a family's newest release first when reversed.

    Digit runs compare as numbers, so ``gpt-10-mini`` outranks ``gpt-9-mini``
    and ``claude-3-haiku`` sorts below ``claude-haiku-4.5``.
    """
    chunks = []
    for index, part in enumerate(_VERSION_CHUNK_RE.split(model_id.lower())):
        if not part:
            continue
        chunks.append((1, float(part), "") if index % 2 else (0, 0.0, part))
    return tuple(chunks)


def is_excluded(model_id: str) -> bool:
    lowered = _NON_THINKING.sub("", model_id.lower())
    return any(x in lowered for x in FAST_EXCLUDE)


def looks_fast(model_id: str) -> bool:
    """True when ``model_id`` belongs to a known small/fast family."""
    lowered = (model_id or "").lower()
    return bool(lowered) and not is_excluded(lowered) and any(r in lowered for r in FAST_RUNGS)


def pick_family(ids: Iterable[str], rungs: Sequence[str] = FAST_RUNGS) -> str:
    """Newest id matching the first rung that matches anything, else ""."""
    ordered = sorted((str(m) for m in ids), key=recency_key, reverse=True)
    for rung in rungs:
        for model_id in ordered:
            if rung in model_id.lower() and not is_excluded(model_id):
                return model_id
    return ""


def _price(value: Any) -> Optional[float]:
    try:
        number = float(str(value).replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def entry_price(entry: Any) -> Optional[float]:
    """Prompt + completion price of one catalog entry, ``None`` when unknown."""
    if not isinstance(entry, Mapping):
        return None
    prompt = _price(entry.get("prompt"))
    completion = _price(entry.get("completion"))
    if prompt is None and completion is None:
        return None
    return (prompt or 0.0) + (completion or 0.0)


def pick_cheapest(catalog: Mapping[str, Any]) -> str:
    """Cheapest priced, chat-capable, non-excluded model ("" without prices).

    Ties go to the newest id.  Only reached when no family rung matched, so
    this is a last-resort price rule, not the primary pick.
    """
    best: Optional[Tuple[float, str]] = None
    for model_id, entry in (catalog or {}).items():
        model_id = str(model_id)
        if is_excluded(model_id):
            continue
        price = entry_price(entry)
        if price is None:
            continue
        if best is None or price < best[0] or (
            price == best[0] and recency_key(model_id) > recency_key(best[1])
        ):
            best = (price, model_id)
    return best[1] if best else ""


# ── speed-probe cache ──────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_probes_in_flight: set = set()
_probes_started: set = set()


def cache_path() -> str:
    from clover_cli.config import get_clover_home

    return str(get_clover_home() / "cache" / "aux_fast_model.json")


def _cache_key(provider: str, base_url: str) -> str:
    return f"{(provider or '').strip().lower()}|{(base_url or '').strip().rstrip('/')}"


def _read_cache() -> Dict[str, Any]:
    try:
        with open(cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data: Dict[str, Any]) -> None:
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        logger.debug("could not write aux fast-model cache", exc_info=True)


def cached_probe(provider: str, base_url: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The cache entry for ``(provider, base_url)`` if still fresh, else ``None``.

    An entry with an empty ``model`` is a remembered "the probe found nothing"
    (shorter TTL) so a provider with no usable candidate is not re-probed on
    every turn.
    """
    now = time.time() if now is None else now
    entry = _read_cache().get(_cache_key(provider, base_url))
    if not isinstance(entry, dict):
        return None
    try:
        age = now - float(entry.get("ts", 0))
    except (TypeError, ValueError):
        return None
    ttl = CACHE_TTL_SECONDS if entry.get("model") else NEGATIVE_TTL_SECONDS
    if age < 0 or age > ttl:
        return None
    return entry


def store_probe(provider: str, base_url: str, model: str, latency_s: Optional[float],
                *, how: str = "probe", now: Optional[float] = None) -> None:
    """Remember what the discovery found (``how``: family | price | probe)."""
    with _cache_lock:
        data = _read_cache()
        data[_cache_key(provider, base_url)] = {
            "model": model,
            "how": how if model else "main",
            "latency_s": round(latency_s, 3) if latency_s is not None else None,
            "ts": time.time() if now is None else now,
        }
        _write_cache(data)


def probe_candidates(ids: Iterable[str], limit: int = PROBE_MAX_CANDIDATES) -> List[str]:
    """At most ``limit`` newest chat models, small families first.

    "The newest chat models": rung-matching ids (in rung order, newest first)
    lead, the rest of the catalog fills in newest-first.
    """
    chat = [str(m) for m in ids if not is_excluded(str(m))]
    ordered = sorted(chat, key=recency_key, reverse=True)
    out: List[str] = []
    for rung in FAST_RUNGS:
        for model_id in ordered:
            if rung in model_id.lower() and model_id not in out:
                out.append(model_id)
                break
        if len(out) >= limit:
            return out[:limit]
    for model_id in ordered:
        if model_id not in out:
            out.append(model_id)
        if len(out) >= limit:
            break
    return out[:limit]


def _valid_probe_reply(text: str) -> bool:
    """A usable reply is a JSON object (a fenced one counts)."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.strip("`").strip()
        if body.lower().startswith("json"):
            body = body[4:]
    try:
        return isinstance(json.loads(body), dict)
    except ValueError:
        return False


def run_probe(
    provider: str,
    base_url: str,
    candidates: Sequence[str],
    call: Callable[[str], Tuple[str, float]],
) -> Tuple[str, Optional[float]]:
    """Time ``call(model) -> (reply_text, seconds)`` on each candidate.

    Returns ``(fastest valid model, seconds)`` or ``("", None)``.  A candidate
    that raises or returns something that is not a JSON object is dropped.
    """
    best: Tuple[str, Optional[float]] = ("", None)
    for model in list(candidates)[:PROBE_MAX_CANDIDATES]:
        try:
            text, seconds = call(model)
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not end the probe
            logger.debug("aux fast-model probe: %s/%s failed: %s", provider, model, exc)
            continue
        if not _valid_probe_reply(text):
            continue
        if best[1] is None or seconds < best[1]:
            best = (model, seconds)
    return best


def start_background_probe(
    provider: str,
    base_url: str,
    list_candidates: Callable[[], Sequence[str]],
    call_factory: Callable[[], Callable[[str], Tuple[str, float]]],
    on_done: Optional[Callable[[], None]] = None,
) -> bool:
    """Probe in a daemon thread, once per process per ``(provider, base_url)``.

    Never blocks the caller.  Returns True when a thread was started.
    """
    key = _cache_key(provider, base_url)
    with _cache_lock:
        if key in _probes_in_flight or key in _probes_started:
            return False
        _probes_in_flight.add(key)
        _probes_started.add(key)

    def _work() -> None:
        try:
            candidates = list(list_candidates())[:PROBE_MAX_CANDIDATES]
            if not candidates:
                store_probe(provider, base_url, "", None)
                return
            model, seconds = run_probe(provider, base_url, candidates, call_factory())
            store_probe(provider, base_url, model, seconds, how="probe")
            logger.info(
                "Auxiliary fast-model probe for %s: %s",
                provider, f"{model} ({seconds:.2f}s)" if model else "no candidate answered",
            )
        except Exception:  # noqa: BLE001
            logger.debug("aux fast-model probe crashed", exc_info=True)
        finally:
            with _cache_lock:
                _probes_in_flight.discard(key)
            if on_done is not None:
                try:
                    on_done()
                except Exception:  # noqa: BLE001
                    logger.debug("aux fast-model probe callback failed", exc_info=True)

    threading.Thread(target=_work, name="aux-fast-model-probe", daemon=True).start()
    return True


def reset_probe_state() -> None:
    """Forget which probes this process already started (tests)."""
    with _cache_lock:
        _probes_in_flight.clear()
        _probes_started.clear()


# ── small-talk allow-list ──────────────────────────────────────────────────
#
# Deliberately a closed vocabulary, never a length rule: maslul's warning that
# difficulty is not readable from length.  Every word of the message must be an
# acknowledgement / greeting / thanks word, or the whole message must match one
# of the explicit phrases.  Work verbs ("restart", "go", "run", "fix", "do"),
# URLs, paths, numbers and unknown words all fall through to the router.

_SMALL_TALK_WORDS = frozenset("""
ok okay okey k kk yes yeah yep yup ya yea no nope nah sure alright aight right
thanks thank thx ty tyvm cheers you u so much very a lot again mate man buddy
great cool nice awesome perfect good fine neat sweet excellent amazing brilliant
fantastic wonderful lovely beautiful lgtm
got noted understood it that appreciate sounds works
lol lmao haha hahaha hehe wow ha
hi hello hey yo sup there morning afternoon evening night gm gn bye goodbye later
see ya cya np problem nvm never mind same too
please pls sorry oops my bad mb
""".split())

_SMALL_TALK_PHRASES = tuple(re.compile(p) for p in (
    r"(?:ok|okay|alright|right|yes|yep|yeah|sure|k)\s+go",
    r"go\s+ahead(?:\s+(?:and|then|please|pls))?",
    r"go\s+for\s+it",
    r"(?:ok|okay|yes|yep|yeah|sure|alright)?\s*go\s+(?:ahead|for it)",
    r"how\s+(?:are|r)\s+(?:you|u)(?:\s+(?:doing|today|going))?",
    r"how\s+(?:is|s)\s+it\s+going",
    r"hows\s+it\s+going",
    r"how\s+do\s+you\s+do",
    r"how\s+u\s+doing",
    r"what\s*s\s+up",
    r"whats\s+up",
    r"(?:ok|okay)?\s*(?:restart(?:ed)?\s+(?:is\s+)?done|done\s+restarting|restart\s+complete)",
    r"all\s+(?:set|good|done)",
    r"(?:that|it)\s+(?:works|worked|is\s+working|s\s+working)(?:\s+now)?",
    r"works\s+now",
    r"looks\s+good(?:\s+to\s+me)?",
    r"sounds\s+good",
    r"good\s+(?:job|work)",
    r"well\s+done",
    r"thanks?\s+(?:for\s+(?:that|the\s+help|your\s+help))",
))
_APOSTROPHES = re.compile(r"['’`´]")
_SMALL_TALK_STRIP = re.compile(r"[^\w\s]", re.UNICODE)
_MAX_SMALL_TALK_WORDS = 8


def is_small_talk(message: Any) -> bool:
    """True for explicit small talk / acknowledgements; False for anything else.

    "ok go", "thanks", "nice", "yes", "lol", "how are you", "restart done" and
    emoji-only messages are small talk.  Anything with a work verb, a URL, a
    path, a digit, or an unknown word ("how is the build going", "use a helper
    to count files") is not, and is routed as before.
    """
    if not isinstance(message, str):
        return False
    text = message.strip()
    if not text or len(text) > 80:
        return False
    lowered = text.lower()
    if "://" in lowered or "www." in lowered or "/" in lowered or "\\" in lowered or "@" in lowered:
        return False
    if "`" in lowered or "~" in lowered:
        return False
    if re.search(r"\d", lowered):
        return False
    folded = _APOSTROPHES.sub("", lowered)
    folded = _SMALL_TALK_STRIP.sub(" ", folded)
    words = folded.split()
    if not words:
        return True  # emoji / punctuation only (nothing word-like left)
    if len(words) > _MAX_SMALL_TALK_WORDS:
        return False
    collapsed = " ".join(words)
    if any(p.fullmatch(collapsed) for p in _SMALL_TALK_PHRASES):
        return True
    return all(w in _SMALL_TALK_WORDS for w in words)
