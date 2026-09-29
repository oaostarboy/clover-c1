"""Live ``🏛 Council`` card that follows the chat and ends as ONE message.

Same look family and same machinery as the subagent board in
``gateway.delegation_activity``:

* one Telegram collapsible quote per run, header always visible;
* the card is registered in the board's ``_LIVE`` registry, so when any newer
  message lands in the chat ``note_outbound`` -> ``follow_latest_message``
  deletes it and re-posts it at the bottom (adapters that cannot delete keep
  editing in place);
* its own sends run under ``_CARD_SEND`` so they never trigger a move;
* every id it ever posted is kept and swept down to the newest one, and the ids
  are persisted so a gateway restart sweeps an orphaned card;
* the run ends as exactly one final message (card + answer together) and the
  live card is deleted.

Seat text arrives per seat: each seat is a one-shot ``clover -z`` agent that
writes its answer file once, so the card updates as each seat file lands
(and reads a partial file's first complete line if one is ever visible).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from gateway import delegation_activity as da

logger = logging.getLogger(__name__)

SEATS_BY_MODE: Dict[str, List[str]] = {
    "quick": ["STEELMAN", "PROSECUTOR", "PRAGMATIST"],
    "full": ["STEELMAN", "PROSECUTOR", "PREMISE", "PRAGMATIST", "OUTSIDER"],
    "deep": ["STEELMAN", "PROSECUTOR", "PREMISE", "PRAGMATIST", "OUTSIDER", "HISTORIAN"],
}
SEAT_EMOJI = {
    "STEELMAN": "🛡️",
    "PROSECUTOR": "⚔️",
    "PREMISE": "🧩",
    "PRAGMATIST": "🔧",
    "OUTSIDER": "👀",
    "HISTORIAN": "📜",
    "CHAIRMAN": "👑",
    "ATTACK": "🗡️",
}
STAGE_LABELS = {
    "arguments": "Opening",
    "cross_review": "Cross-review",
    "chairman": "Chairman",
    "attack": "Attack",
    "ruling": "Ruling",
}
TAP_HINT_LIVE = "tap to watch 🏛"
TAP_HINT_DONE = "tap to read 🏛"

QUESTION_MAX = 160
TAKE_MAX = 42
# At most one card edit per this many seconds, overall.
MIN_EDIT_SECONDS = 2.0
# A change that is only a ticking clock (header / "thinking… Ns") is not worth
# an edit sooner than this.
CLOCK_ONLY_EDIT_SECONDS = 10.0

SPACER = "\u2800"  # same invisible spacer the subagent board uses

_STORE_PREFIX = "council:"
_MODEL_FAMILIES = (("gemini", "Gemini"), ("grok", "Grok"), ("claude-fable", "Fable"))


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


# ── text helpers ───────────────────────────────────────────────────────────

_PAREN_PATH_RE = re.compile(r"\([^()]*(?:/|\.md\b|\.txt\b)[^()]*\)")
_PATH_TOKEN_RE = re.compile(r"(?:~|\.{1,2})?/\S+|\S+\.(?:md|txt|json|py)\b")
_MD_RE = re.compile(r"[*_`#>]+")


def _trim(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit + 1].rsplit(" ", 1)[0] if " " in text[: limit + 1] else text[:limit]
    return cut.rstrip(" .,;:!?-") + "…"


def clean_question(question: str) -> str:
    """The user's question for the card: no file paths, ~160 chars."""
    text = _PAREN_PATH_RE.sub(" ", str(question or ""))
    text = _PATH_TOKEN_RE.sub(" ", text)
    text = text.replace("*", "")
    text = re.sub(r"\(\s*\)", " ", text)
    return _trim(text, QUESTION_MAX)


def seat_take(text: str, limit: int = TAKE_MAX, final: bool = False) -> str:
    """One phone-width line from a seat file: its ``GIST:`` else its first line.

    While a file is still being written its last line may be partial, so only
    complete lines count until the ``.done`` marker (``final``) appears.
    """
    if not text or not text.strip():
        return ""
    lines = text.splitlines()
    if not final and not text.endswith("\n"):
        lines = lines[:-1]
    for line in lines:
        match = re.match(r"^\**\s*GIST\s*:\**\s*(.+)$", line.strip(), re.I)
        if match:
            return _trim(_MD_RE.sub("", match.group(1)), limit)
    first = next((ln for ln in lines if ln.strip()), "")
    return _trim(_MD_RE.sub("", first), limit) if first else ""


def pretty_seat_model(model: str) -> str:
    """``gpt-5.6-sol`` -> ``GPT-5.6 Sol``; never a provider name."""
    from agent.delegation_activity import pretty_model

    low = str(model or "").lower()
    for prefix, family in _MODEL_FAMILIES:
        if low.startswith(prefix):
            rest = low[len(prefix):].lstrip("-")
            if prefix == "claude-fable":
                nums = [p for p in re.split(r"[-.]", rest) if p.isdigit()]
                return f"{family} {'.'.join(nums)}".strip()
            words = rest.split("-")
            return f"{family} {' '.join(w.capitalize() if not w[:1].isdigit() else w for w in words)}".strip()
    return pretty_model(model)


def load_seat_models(home: Optional[Path]) -> Dict[str, str]:
    """``{SEAT: pretty model}`` from the installed skill's roster."""
    candidates: List[Path] = []
    if home is not None:
        candidates.append(
            Path(home) / "skills" / "autonomous-ai-agents" / "council" / "references" / "models.json"
        )
    candidates.append(
        Path(__file__).resolve().parents[1]
        / "optional-skills" / "autonomous-ai-agents" / "council" / "references" / "models.json"
    )
    for path in candidates:
        data = _read_json(path)
        if data:
            return {
                str(seat): pretty_seat_model(str(entry.get("model") or ""))
                for seat, entry in data.items()
                if isinstance(entry, dict)
            }
    return {}


def read_question(work: Path) -> str:
    """The run's question: ``question.txt`` (new runner) else ``report.md``."""
    text = _read_text(work / "question.txt").strip()
    if text:
        return text
    report = _read_text(work / "report.md")
    match = re.search(r"^Question:\s*(.*?)\n(?:Mode:|Seats:)", report, re.S | re.M)
    return match.group(1).strip() if match else ""


def elapsed_label(seconds: Any) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if total >= 600:
        return f"{total // 60}m"
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


# ── rendering ──────────────────────────────────────────────────────────────


def _seat_block(seat: str, models: Mapping[str, str], take: str) -> List[str]:
    label = seat.title()
    model = models.get(seat, "")
    head = f"{SEAT_EMOJI.get(seat, '•')} **{label}**" + (f" · {model}" if model else "")
    return [head, f"*{take}*" if take else ""]


def _stage_line(state: Mapping[str, Any], mode: str) -> str:
    stage = str(state.get("stage") or "arguments")
    status = str(state.get("status") or "running")
    order = ["arguments"] + (["cross_review"] if mode != "quick" else []) + ["chairman"]
    if mode == "deep":
        order.append("attack")
    rank = {name: i for i, name in enumerate(order + ["ruling"])}
    rank["ruling"] = rank.get("attack", len(order))
    current = rank.get(stage, 0)

    def counts(stem: str) -> str:
        try:
            done = int(state.get(f"{stem}_done") or 0)
            total = int(state.get(f"{stem}_total") or 0)
        except (TypeError, ValueError):
            return ""
        return f" {min(done, total) if total else done}/{total}" if total else ""

    stalled = [s for s in (state.get("stalled") or []) if s]
    parts = []
    for name in order:
        idx = rank[name]
        if status == "done" or idx < current:
            mark = "⚠" if name == "arguments" and stalled else "✓"
        elif idx == current:
            mark = "◉" if status == "running" else ("⚠" if status == "failed" else "✓")
        else:
            mark = "○"
        extra = counts("seat") if name == "arguments" else counts("review") if name == "cross_review" else ""
        parts.append(f"{mark} {STAGE_LABELS[name]}{extra}")
    return " · ".join(parts)


def wrap_quote(header: str, body: List[str], *, expandable: bool, hint: str) -> str:
    """Header line + quoted body; Telegram gets the collapsible quote."""
    lines = [header] + [f"> {ln}" for ln in body if ln]
    text = "\n".join(lines)
    if expandable:
        return da._to_expandable(text, hint)
    return text


def render_live(snap: "Snapshot", *, expandable: bool, times: bool = True) -> str:
    state = snap.state
    mode = snap.mode
    header = f"🏛 Council · {mode.title()}" + (f" · ⏱ {elapsed_label(state.get('elapsed_s'))}" if times else "")
    body: List[str] = []
    if snap.question:
        body += [f"❓ **{snap.question}**", SPACER]
    body += [_stage_line(state, mode), SPACER]
    blocks: List[List[str]] = []
    for seat in snap.seats:
        blocks.append(_seat_block(seat.name, snap.models, seat.line(times)))
    for extra in snap.extra_seats:
        blocks.append(_seat_block(extra.name, snap.models, extra.line(times)))
    for i, block in enumerate(blocks):
        if i:
            body.append(SPACER)
        body += block
    return wrap_quote(header, body, expandable=expandable, hint=TAP_HINT_LIVE)


def render_final(
    snap: "Snapshot",
    summary: Optional[Mapping[str, Any]],
    *,
    expandable: bool,
    failed_stage: str = "",
    limit: int = 4096,
) -> str:
    from gateway.council_progress import format_council_result

    ok = summary is not None and not failed_stage
    elapsed = (summary or {}).get("elapsed_s") if summary else None
    if elapsed is None:
        elapsed = snap.state.get("elapsed_s")
    header = f"{'✅' if ok else '❌'} 🏛 Council · {snap.mode.title()} · ⏱ {elapsed_label(elapsed)}"
    body: List[str] = []
    if snap.question:
        body += [f"❓ **{snap.question}**", SPACER]
    for i, seat in enumerate(snap.seats):
        if i:
            body.append(SPACER)
        body += _seat_block(seat.name, snap.models, seat.final_line())
    if not ok:
        body += [SPACER, f"⚠ Failed at {failed_stage or 'the run'}"]
    outside = format_council_result(summary, header=False) if ok else (
        f"**Council failed at {failed_stage or 'the run'}.**\n"
        "The stage is preserved in the run report."
    )
    # Fit the quote into whatever the answer leaves of the message limit.
    budget = max(400, limit - len(outside) - 8)
    while True:
        quote = wrap_quote(header, body, expandable=expandable, hint=TAP_HINT_DONE)
        if len(quote) <= budget or len(body) <= 4:
            break
        body = body[:-3]
    return f"{quote}\n\n{outside}"


# ── run snapshot (progress.json + seat files) ──────────────────────────────


class SeatView:
    def __init__(self, name: str, *, text1: str, done1: bool, text2: str, done2: bool,
                 stalled: bool, stage: str, waited_s: float, reviewing: bool) -> None:
        self.name = name
        self.text1, self.done1, self.text2, self.done2 = text1, done1, text2, done2
        self.stalled, self.stage, self.waited_s, self.reviewing = stalled, stage, waited_s, reviewing

    def _think(self, times: bool, verb: str = "thinking") -> str:
        return f"{verb}… {int(self.waited_s)}s" if times else f"{verb}…"

    def line(self, times: bool = True) -> str:
        if self.stalled:
            return "⚠ no answer (stalled)"
        if self.stage == "cross_review" and self.reviewing:
            review = seat_take(self.text2, final=self.done2)
            if review:
                return "review: " + review
            return self._think(times, "reviewing")
        take = seat_take(self.text1, final=self.done1)
        if take:
            return take
        return self._think(times)

    def final_line(self) -> str:
        take = seat_take(self.text1, final=True)
        if self.stalled or not take:
            return "⚠ no answer (stalled)"
        return take


class ExtraSeat:
    """Chairman / attack rows, shown only while they are working."""

    def __init__(self, name: str, waited_s: float, text: str = "") -> None:
        self.name, self.waited_s, self.text = name, waited_s, text

    def line(self, times: bool = True) -> str:
        take = seat_take(self.text)
        if take:
            return take
        return f"thinking… {int(self.waited_s)}s" if times else "thinking…"


class Snapshot:
    def __init__(self, state: Mapping[str, Any], mode: str, question: str,
                 models: Mapping[str, str], seats: List[SeatView], extra_seats: List[ExtraSeat]) -> None:
        self.state, self.mode, self.question = state, mode, question
        self.models, self.seats, self.extra_seats = models, seats, extra_seats


def build_snapshot(
    work: Path,
    state: Mapping[str, Any],
    *,
    mode: str,
    question: str,
    models: Mapping[str, str],
    stage_started: Mapping[str, float],
    now: float,
) -> Snapshot:
    stage = str(state.get("stage") or "arguments").lower()
    status = str(state.get("status") or "running").lower()
    stalled = {str(s) for s in (state.get("stalled") or []) if s}
    seats: List[SeatView] = []
    for name in SEATS_BY_MODE.get(mode, SEATS_BY_MODE["full"]):
        f1, f2 = work / f"stage1-{name}.md", work / f"stage2-{name}.md"
        d1, d2 = (work / f"stage1-{name}.md.done").exists(), (work / f"stage2-{name}.md.done").exists()
        t1, t2 = _read_text(f1), _read_text(f2)
        got1 = d1 or bool(seat_take(t1))
        lost = name in stalled and not got1
        # In cross-review only seats that answered are reviewing.
        seats.append(SeatView(
            name, text1=t1, done1=d1, text2=t2, done2=d2, stalled=lost, stage=stage,
            waited_s=max(0.0, now - stage_started.get(stage, now)),
            reviewing=got1,
        ))
    extras: List[ExtraSeat] = []
    if status == "running":
        if stage == "chairman":
            extras.append(ExtraSeat("CHAIRMAN", max(0.0, now - stage_started.get(stage, now))))
        elif stage == "attack":
            extras.append(ExtraSeat("ATTACK", max(0.0, now - stage_started.get(stage, now))))
        elif stage == "ruling":
            extras.append(ExtraSeat("CHAIRMAN", max(0.0, now - stage_started.get(stage, now))))
    return Snapshot(state, mode, clean_question(question), models, seats, extras)


# ── the card ───────────────────────────────────────────────────────────────


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# Persisted ids of live council cards, per chat: {store_key: {run_id: [ids]}}.
_CARD_IDS: Dict[str, Dict[str, List[str]]] = {}
_ORPHANS_SWEPT: set = set()


class CouncilLiveCard:
    """One live council card in one chat (see module docstring)."""

    owns_final = True

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        run_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
        *,
        work: Path,
        question: str,
        mode: str,
        models: Optional[Mapping[str, str]] = None,
        expandable: bool = True,
        clock: Callable[[], float] = time.time,
        min_edit: float = MIN_EDIT_SECONDS,
    ) -> None:
        self.adapter = adapter
        self.chat_id = str(chat_id)
        self.run_id = run_id
        self.metadata = metadata
        self.work = Path(work)
        self.question = question
        self.mode = mode if mode in SEATS_BY_MODE else "full"
        self.models = dict(models or {})
        self.expandable = expandable
        self._clock = clock
        self._min_edit = min_edit
        self.message_id: Optional[str] = None
        self.last_text = ""
        self.last_struct = ""
        self.last_edit = float("-inf")
        self._posted: List[str] = []
        self._state: Mapping[str, Any] = {}
        self._stage_started: Dict[str, float] = {}
        self._es_value = -1
        self._es_seen = 0.0
        self._lock: Optional[asyncio.Lock] = None
        self._timer: Any = None
        self._loop: Any = None
        self._closed = False
        self.final_delivered = False
        self._combined = False  # never part of the subagent board
        self._store_key = _STORE_PREFIX + da._board_store_key(adapter, chat_id)

    # -- follow protocol (called by delegation_activity.follow_latest_message)

    @property
    def key(self) -> Tuple[int, str]:
        return da._inbox_key(self.adapter, self.chat_id)

    def _register(self) -> None:
        if self._closed:
            return
        with da._LIVE_LOCK:
            pubs = da._LIVE.setdefault(self.key, [])
            if self not in pubs:
                pubs.append(self)

    def _unregister(self) -> None:
        with da._LIVE_LOCK:
            pubs = da._LIVE.get(self.key)
            if pubs and self in pubs:
                pubs.remove(self)
                if not pubs:
                    da._LIVE.pop(self.key, None)

    async def repost_live_cards(self) -> int:
        """Delete the live card and post it again at the bottom of the chat."""
        if self._closed:
            return 0
        return int(await self.refresh(move=True))

    # -- persistence -------------------------------------------------------

    def _persist(self) -> None:
        runs = _CARD_IDS.setdefault(self._store_key, {})
        if self._posted:
            runs[self.run_id] = list(self._posted)
        else:
            runs.pop(self.run_id, None)
        flat = [i for ids in runs.values() for i in ids]
        da._board_store_set(self._store_key, flat)

    def _adopt_orphans(self) -> None:
        """Ids left in the store by a previous gateway process are stale."""
        if self._store_key in _ORPHANS_SWEPT:
            return
        _ORPHANS_SWEPT.add(self._store_key)
        try:
            for mid in da._board_store_load().get(self._store_key, []):
                if mid not in self._posted:
                    self._posted.append(mid)
        except Exception:
            pass

    async def _delete(self, message_id: str) -> bool:
        try:
            if da._adapter_can_delete(self.adapter):
                return bool(await _maybe_await(self.adapter.delete_message(self.chat_id, message_id)))
        except Exception:
            logger.debug("council card delete failed", exc_info=True)
        return False

    async def _sweep_except(self, keep: Optional[str]) -> None:
        stale = [m for m in self._posted if m != keep]
        for mid in stale:
            if await self._delete(mid):
                self._posted.remove(mid)
        self._persist()

    # -- state -------------------------------------------------------------

    def _snapshot(self, state: Mapping[str, Any]) -> Snapshot:
        now = self._clock()
        stage = str(state.get("stage") or "arguments").lower()
        self._stage_started.setdefault(stage, now)
        mode = str(state.get("mode") or self.mode).lower()
        if mode in SEATS_BY_MODE:
            self.mode = mode
        # progress.json is rewritten per stage step, so tick the clock between.
        try:
            reported = int(state.get("elapsed_s") or 0)
        except (TypeError, ValueError):
            reported = 0
        if reported != self._es_value:
            self._es_value, self._es_seen = reported, now
        state = dict(state, elapsed_s=reported + max(0.0, now - self._es_seen))
        return build_snapshot(
            self.work, state, mode=self.mode, question=self.question,
            models=self.models, stage_started=self._stage_started, now=now,
        )

    async def publish(self, state: Mapping[str, Any]) -> None:
        self._state = dict(state)
        await self.refresh()

    update = publish

    def _schedule(self, delay: float) -> None:
        if self._timer is not None or self._closed:
            return
        loop = self._loop or asyncio.get_running_loop()

        def _go() -> None:
            self._timer = None
            loop.create_task(self.refresh())

        self._timer = loop.call_later(max(0.0, delay), _go)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def refresh(self, *, move: bool = False) -> bool:
        """Post, edit or move the card. Returns True if it moved."""
        if self._closed:
            return False
        if self._lock is None:
            self._lock = asyncio.Lock()
            self._loop = asyncio.get_running_loop()
        token = da._CARD_SEND.set(True)
        try:
            async with self._lock:
                if self._closed:
                    return False
                snap = self._snapshot(self._state)
                text = render_live(snap, expandable=self.expandable)
                struct = render_live(snap, expandable=self.expandable, times=False)
                limit = self._limit()
                text = da._fit_to_limit(text, limit)
                last_out = da._LAST_OUT.get(self.key)
                behind = bool(self.message_id and last_out and da._is_newer(last_out, self.message_id))
                if self.message_id is None or (move and behind):
                    return await self._post(text, struct)
                if text == self.last_text:
                    return False
                now = self._clock()
                changed = struct != self.last_struct
                wait = self.last_edit + (self._min_edit if changed else CLOCK_ONLY_EDIT_SECONDS) - now
                if wait > 0:
                    self._schedule(wait)
                    return False
                await self._edit(text, struct)
                return False
        except Exception:
            logger.debug("council card refresh failed", exc_info=True)
            return False
        finally:
            da._CARD_SEND.reset(token)

    def _limit(self) -> int:
        try:
            limit = int(getattr(self.adapter, "MAX_MESSAGE_LENGTH", 4096) or 4096)
        except Exception:
            limit = 4096
        return limit - (8 if self.expandable else 0)

    async def _post(self, text: str, struct: str) -> bool:
        self._adopt_orphans()
        old = self.message_id
        res = await _maybe_await(self.adapter.send(self.chat_id, text, metadata=self.metadata))
        new_id = getattr(res, "message_id", None)
        if not (getattr(res, "success", False) and new_id):
            return False
        self.message_id = str(new_id)
        self.last_text, self.last_struct = text, struct
        self.last_edit = self._clock()
        self._posted.append(self.message_id)
        self._register()
        await self._sweep_except(self.message_id)
        return bool(old)

    async def _edit(self, text: str, struct: str) -> bool:
        editor = getattr(self.adapter, "edit_message", None)
        if not callable(editor):
            return False
        res = await _maybe_await(editor(
            self.chat_id, self.message_id, text, finalize=True, metadata=self.metadata,
        ))
        if getattr(res, "success", False):
            self.last_text, self.last_struct = text, struct
            self.last_edit = self._clock()
            return True
        if da._message_gone(res):
            self.message_id = None
            self._schedule(0.0)
        return False

    async def follow(
        self,
        progress_path: Path,
        finished: Callable[[], bool],
        last_state: Mapping[str, Any],
        *,
        poll_s: float = 1.0,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> dict:
        """Mirror ``progress.json`` + seat files onto the card until ``finished()``."""
        state = dict(last_state)
        while True:
            done = finished()
            fresh = _read_json(progress_path)
            if fresh is not None:
                state = fresh
                if not done:
                    await self.publish(state)
            if done:
                return state
            await (sleep or asyncio.sleep)(poll_s)

    # -- ending ------------------------------------------------------------

    async def finish_done(self, state: Mapping[str, Any], summary: Mapping[str, Any]) -> None:
        await self._finish(state, summary, "")

    async def finish_failed(self, state: Mapping[str, Any]) -> None:
        stage = str(state.get("stage") or "the run").lower()
        await self._finish(state, None, STAGE_LABELS.get(stage, stage.replace("_", " ")))

    async def _finish(self, state: Mapping[str, Any], summary: Optional[Mapping[str, Any]], failed: str) -> None:
        """Exactly one final message; the live card is gone (or IS that message)."""
        if self._closed:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
            self._loop = asyncio.get_running_loop()
        self._cancel_timer()
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._unregister()
            self._adopt_orphans()
            snap = self._snapshot(dict(state))
            text = render_final(
                snap, summary, expandable=self.expandable,
                failed_stage=failed, limit=self._limit() + 8,
            )
            can_delete = da._adapter_can_delete(self.adapter)
            delivered: Optional[str] = None
            token = da._CARD_SEND.set(False)
            try:
                if can_delete or not self.message_id:
                    res = await _maybe_await(self.adapter.send(self.chat_id, text, metadata=self.metadata))
                    if getattr(res, "success", False):
                        delivered = str(getattr(res, "message_id", "") or "sent")
                elif not can_delete and self.message_id:
                    # On immutable-message adapters preserve the sole live post.
                    pass
                if delivered is None and self.message_id:
                    editor = getattr(self.adapter, "edit_message", None)
                    if callable(editor):
                        res = await _maybe_await(editor(
                            self.chat_id, self.message_id, text, finalize=True, metadata=self.metadata,
                        ))
                        if getattr(res, "success", False):
                            delivered = self.message_id

            except Exception:
                logger.warning("Council final message for %s failed", self.run_id, exc_info=True)
            finally:
                da._CARD_SEND.reset(token)
            # Failed final sends must not leave an obsolete live card beside
            # the /council handler's normal reply.
            keep = self.message_id if delivered == self.message_id else None
            await self._sweep_except(keep)
            self.message_id = keep
            self.final_delivered = delivered is not None
