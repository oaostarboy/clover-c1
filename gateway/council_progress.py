"""Compact council-specific progress cards for gateway surfaces."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional
import re

logger = logging.getLogger(__name__)


_MODES = {"quick", "full", "deep"}
_STAGE_LABELS = {
    "arguments": "Opening arguments",
    "cross_review": "Cross-review",
    "chairman": "Chairman",
    "attack": "Verdict attack",
    "ruling": "Chairman ruling",
}


def parse_council_args(raw: str) -> tuple[str, str]:
    """Return ``(mode, exact question)`` from gateway command arguments."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Usage: /council [quick|full|deep] <question>")
    first, separator, rest = text.partition(" ")
    if first.lower() in _MODES:
        question = rest.strip() if separator else ""
        if not question:
            raise ValueError("Usage: /council [quick|full|deep] <question>")
        return first.lower(), question
    return "full", text


def format_elapsed(seconds: Any) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _count(state: Mapping[str, Any], stem: str) -> tuple[int, int]:
    try:
        done = max(0, int(state.get(f"{stem}_done", 0) or 0))
    except (TypeError, ValueError):
        done = 0
    try:
        total = max(0, int(state.get(f"{stem}_total", 0) or 0))
    except (TypeError, ValueError):
        total = 0
    return min(done, total) if total else done, total


def render_council_card(state: Mapping[str, Any]) -> str:
    """Render one small live or collapsed council card."""
    status = str(state.get("status") or "running").lower()
    mode = str(state.get("mode") or "full").lower()
    stage = str(state.get("stage") or "arguments").lower()
    elapsed = format_elapsed(state.get("elapsed_s"))
    seat_done, seat_total = _count(state, "seat")
    review_done, review_total = _count(state, "review")

    if status == "done":
        parts = ["🏛 Council"]
        if seat_total:
            parts.append(f"{seat_done} seats")
        if review_total:
            parts.append(f"{review_done} reviews")
        parts.append(elapsed)
        return " · ".join(parts)

    if status == "failed":
        label = _STAGE_LABELS.get(stage, stage.replace("_", " ").title() or "Unknown")
        return f"🏛 Council failed · {label} · {elapsed}"

    lines = [f"🏛 Council · {mode.title()}"]
    argument_text = (
        f"Opening arguments {seat_done}/{seat_total}"
        if seat_total
        else "Opening arguments"
    )
    review_text = (
        f"Cross-review {review_done}/{review_total}"
        if review_total
        else "Cross-review"
    )

    if stage == "arguments":
        lines.extend([f"◉ {argument_text}", "○ Cross-review", "○ Chairman"])
    elif stage == "cross_review":
        lines.extend([f"✓ {argument_text}", f"◉ {review_text}", "○ Chairman"])
    else:
        lines.append(f"✓ {argument_text}")
        if mode != "quick":
            lines.append(f"✓ {review_text}")
        lines.append(f"{'◉' if stage == 'chairman' else '✓'} Chairman")
        if mode == "deep":
            lines.append(
                f"{'◉' if stage == 'attack' else '✓' if stage == 'ruling' else '○'} "
                "Verdict attack"
            )
            if stage == "ruling":
                lines.append("◉ Chairman ruling")
    return "\n".join(lines)


def format_council_result(summary: Mapping[str, Any], *, header: bool = True) -> str:
    """Format the actual reply; the compact card deliberately omits the verdict.

    ``header=False`` drops the ``🏛 Council answer`` title line, for the v2
    final message whose quote header already says it.
    """
    mode = str(summary.get("mode") or "full").title()
    verdict = _compact_council_text(
        summary.get("verdict") or "No verdict returned.",
        max_chars=180,
        max_sentences=2,
    )
    why = _compact_council_text(
        summary.get("why") or summary.get("next") or "No reason returned.",
        max_chars=120,
        max_sentences=1,
    )
    caveat = _compact_council_text(
        summary.get("caveat") or summary.get("dissent") or "No caveat returned.",
        max_chars=150,
        max_sentences=1,
    )
    stalled = [str(item) for item in (summary.get("stalled") or []) if item]
    seat_note = (
        "stalled: " + ", ".join(stalled)
        if stalled
        else "all seats returned"
    )
    title = "🏛 **Council answer**\n\n" if header else ""
    return (
        f"{title}"
        "**Answer**\n"
        f"• {verdict}\n\n"
        "**Why**\n"
        f"• {why}\n\n"
        "**What could change it**\n"
        f"• {caveat}\n\n"
        f"*{mode} council · {seat_note}*"
    )


def _compact_council_text(value: Any, *, max_chars: int, max_sentences: int) -> str:
    """Turn model prose into a bounded mobile-friendly field."""
    text = " ".join(str(value or "").split())
    if not text:
        return "Not provided."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    selected = " ".join(sentences[:max_sentences]).strip()
    omitted = len(sentences) > max_sentences
    if len(selected) > max_chars:
        selected = selected[: max_chars + 1].rsplit(" ", 1)[0].rstrip(" .!?;:")
        omitted = True
    if omitted:
        selected = selected.rstrip(" .!?;:") + "…"
    return selected


# ---------------------------------------------------------------------------
# Live card driver (shared by /council and agent-launched runs)
# ---------------------------------------------------------------------------

# Run ids whose card is owned by a /council handler in this process. The run
# watcher must never open a second card for them.
_MANAGED_RUN_IDS: set[str] = set()


def mark_council_run_managed(run_id: str) -> None:
    _MANAGED_RUN_IDS.add(run_id)


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class CouncilCard:
    """One editable ``🏛 Council`` card in one chat; repeat renders are no-ops."""

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        status_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.adapter = adapter
        self.chat_id = str(chat_id)
        self.status_key = status_key
        self.metadata = metadata
        self._last_card = ""
        self.final_delivered = False
        self.final_maybe_delivered = False

    async def publish(self, state: Mapping[str, Any]) -> None:
        if self.adapter is None:
            return
        card = render_council_card(state)
        if card == self._last_card:
            return
        first_card = not self._last_card
        self._last_card = card
        updater = getattr(self.adapter, "send_or_update_status", None)
        if callable(updater):
            result = updater(
                self.chat_id, self.status_key, card, metadata=self.metadata
            )
        elif first_card:
            result = self.adapter.send(self.chat_id, card, metadata=self.metadata)
        else:
            return
        if inspect.isawaitable(result):
            await result

    # v2 cards own the final message; the classic card leaves it to the caller.
    owns_final = False

    async def finish_done(self, state: Mapping[str, Any], summary: Mapping[str, Any]) -> None:
        """Collapse the card, then post the formatted answer as its own message."""
        await self.publish(state)
        sent = self.adapter.send(
            self.chat_id, format_council_result(summary), metadata=self.metadata
        )
        if inspect.isawaitable(sent):
            sent = await sent
        self.final_delivered = bool(getattr(sent, "success", False))

    async def finish_failed(self, state: Mapping[str, Any]) -> None:
        failed = dict(state)
        failed["status"] = "failed"
        await self.publish(failed)

    async def follow(
        self,
        progress_path: Path,
        finished: Callable[[], bool],
        last_state: Mapping[str, Any],
        *,
        poll_s: float = 1.0,
        sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
    ) -> dict:
        """Mirror ``progress.json`` onto the card until ``finished()``.

        Returns the last state seen (re-read once after ``finished``).
        """
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


CARD_STYLES = ("v2", "classic")


def resolve_card_style(value: Any) -> str:
    """``display.council_card``: ``v2`` (default) or ``classic``."""
    text = str(value if value is not None else "v2").strip().lower()
    return text if text in CARD_STYLES else "v2"


def make_card(
    style: str,
    adapter: Any,
    chat_id: str,
    run_id: str,
    metadata: Optional[Mapping[str, Any]] = None,
    *,
    work: Path,
    question: str = "",
    mode: str = "full",
    expandable: bool = True,
    home: Optional[Path] = None,
):
    """The one card implementation for /council and agent-launched runs."""
    if resolve_card_style(style) == "classic":
        return CouncilCard(adapter, chat_id, f"council:{run_id}", metadata)
    from gateway.council_card import CouncilLiveCard, load_seat_models, read_question

    if home is None:
        try:
            home = Path(work).parents[2]
        except IndexError:
            home = None
    return CouncilLiveCard(
        adapter,
        chat_id,
        run_id,
        metadata,
        work=Path(work),
        question=question or read_question(Path(work)),
        mode=mode,
        models=load_seat_models(home),
        expandable=expandable,
    )


def _pid_alive(pid: Any) -> bool:
    # Never os.kill(pid, 0): on Windows it sends CTRL_C_EVENT to the target.
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(int(pid)))
    except (ValueError, TypeError):
        return True
    except Exception:
        return True


GATEWAY_ACK = "gateway-card.json"
GATEWAY_UNCERTAIN = "gateway-card-uncertain.json"
GATEWAY_FAILED = "gateway-card-failed.json"


def _write_gateway_ack(work: Path) -> None:
    """Mark a run as delivered by the gateway (read by the runner at exit)."""
    try:
        (Path(work) / GATEWAY_ACK).write_text(
            json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8"
        )
    except OSError:
        logger.debug("could not write %s", GATEWAY_ACK, exc_info=True)


def _failure_delivery(card: Any) -> str:
    """Was the failure card shown? The classic card edits a status in place."""
    if not getattr(card, "owns_final", False) or getattr(card, "final_delivered", False):
        return "shown"
    return "uncertain" if getattr(card, "final_maybe_delivered", False) else "not_shown"


class CouncilRunWatcher:
    """Give agent-launched council runs the same live card as ``/council``.

    The runner records ``origin.json`` (chat + session) in its run directory
    when it is started from a gateway turn. Each ``scan_once`` picks up new
    runs that are still running and have no card yet, and drives one card per
    run in the originating chat, then posts the formatted result.
    """

    MAX_RUN_AGE_S = 6 * 3600
    STALE_PROGRESS_S = 45 * 60

    def __init__(
        self,
        *,
        homes: Callable[[], Iterable[Path]],
        resolve_target: Callable[[dict], Optional[tuple[Any, str, Optional[Mapping[str, Any]]]]],
        poll_s: float = 1.0,
        managed: Optional[set[str]] = None,
        card_style: Optional[Callable[[dict], str]] = None,
        on_delivered: Optional[Callable[..., Awaitable[Any]]] = None,
        on_discovered: Optional[Callable[[dict, Path], Any]] = None,
    ) -> None:
        self._homes = homes
        # Awaited once a card is shown (or failed); records it in the
        # launching conversation. See gateway/council_context.py.
        self._on_delivered = on_delivered
        # Called once per newly discovered run, so the gateway can bind an
        # origin that has no session_id to the session live right now.
        self._on_discovered = on_discovered
        self._card_style = card_style or (lambda origin: "v2")
        self._resolve_target = resolve_target
        self._poll_s = poll_s
        self._managed = _MANAGED_RUN_IDS if managed is None else managed
        self._seen: set[Path] = set()
        self.tasks: set[asyncio.Task] = set()

    def scan_once(self) -> list[asyncio.Task]:
        started: list[asyncio.Task] = []
        for home in self._homes():
            runs = Path(home) / "council" / "runs"
            try:
                entries = [e for e in os.scandir(runs) if e.is_dir()]
            except OSError:
                continue
            for entry in entries:
                work = Path(entry.path)
                if work in self._seen:
                    continue
                task = self._consider(work)
                if task is not None:
                    started.append(task)
        return started

    def _consider(self, work: Path) -> Optional[asyncio.Task]:
        run_id = work.name
        if run_id in self._managed:
            self._seen.add(work)
            return None
        origin = _read_json(work / "origin.json")
        if origin is None:
            # Runner may not have written it yet; a CLI run never will.
            try:
                if time.time() - work.stat().st_mtime > 60:
                    self._seen.add(work)
            except OSError:
                self._seen.add(work)
            return None
        self._seen.add(work)
        state = _read_json(work / "progress.json") or {}
        try:
            age = time.time() - float(origin.get("created_at") or 0)
        except (TypeError, ValueError):
            age = self.MAX_RUN_AGE_S + 1
        if age > self.MAX_RUN_AGE_S:
            return None
        if self._on_discovered is not None:
            try:
                self._on_discovered(origin, work)
            except Exception:
                logger.warning("Council discovery hook for %s failed", run_id, exc_info=True)
        shown = self._shown_delivery(work)
        if shown is not None:
            # The card was already handled (possibly by a previous gateway
            # process): never replay it, but a missing context receipt is owed.
            return self._resume_context(work, origin, state, *shown)
        status = str(state.get("status") or "running")
        if status != "running" and not (status == "done" and _read_json(work / "summary.json")):
            return None
        target = self._resolve_target(origin)
        if target is None:
            return None
        adapter, chat_id, metadata = target
        card = make_card(
            self._card_style(origin),
            adapter,
            chat_id,
            run_id,
            metadata,
            work=work,
            mode=str(state.get("mode") or "full"),
            expandable=str(origin.get("platform") or "") == "telegram",
            home=Path(work).parents[2],
        )
        task = asyncio.ensure_future(self._drive(card, work, origin, state))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    @staticmethod
    def _shown_delivery(work: Path) -> Optional[tuple[str, str]]:
        """``(kind, delivery)`` of a card this run already finished, else None."""
        if (work / GATEWAY_ACK).exists():
            return "result", "shown"
        if (work / GATEWAY_UNCERTAIN).exists():
            return "result", "uncertain"
        if (work / GATEWAY_FAILED).exists():
            marker = _read_json(work / GATEWAY_FAILED) or {}
            return "failure", str(marker.get("delivery") or "shown")
        return None

    def _resume_context(
        self, work: Path, origin: dict, state: dict, kind: str, delivery: str
    ) -> Optional[asyncio.Task]:
        from gateway.council_context import CONTEXT_RECEIPT

        if self._on_delivered is None or (work / CONTEXT_RECEIPT).exists():
            return None
        summary = _read_json(work / "summary.json")
        if kind == "result" and summary is None:
            return None
        task = asyncio.ensure_future(
            self._record_context(origin, work, kind, delivery, summary, state)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _record_context(
        self,
        origin: dict,
        work: Path,
        kind: str,
        delivery: str,
        summary: Optional[Mapping[str, Any]],
        state: Mapping[str, Any],
    ) -> None:
        if self._on_delivered is None:
            return
        try:
            created = float(origin.get("created_at") or 0) or time.time()
            await self._on_delivered(
                origin=origin,
                work=work,
                kind=kind,
                delivery=delivery,
                summary=summary,
                state=state,
                deadline=created + self.MAX_RUN_AGE_S,
            )
        except Exception:
            logger.warning("Council context for %s failed", work.name, exc_info=True)

    def _runner_gone(self, work: Path, origin: Mapping[str, Any]) -> bool:
        if origin.get("host") == socket.gethostname() and origin.get("pid"):
            if not _pid_alive(origin["pid"]):
                return True
        try:
            return time.time() - (work / "progress.json").stat().st_mtime > self.STALE_PROGRESS_S
        except OSError:
            return False

    async def _drive(
        self, card: CouncilCard, work: Path, origin: dict, state: dict
    ) -> None:
        progress = work / "progress.json"

        def finished() -> bool:
            current = _read_json(progress) or {}
            if str(current.get("status") or "running") != "running":
                return True
            return self._runner_gone(work, origin)

        try:
            await card.publish(state)
            last = await card.follow(progress, finished, state, poll_s=self._poll_s)
            status = str(last.get("status") or "running")
            summary = _read_json(work / "summary.json")
            if status == "done" and summary is not None:
                await card.finish_done(last, summary)
                if card.final_delivered:
                    _write_gateway_ack(work)
                    delivery = "shown"
                elif card.final_maybe_delivered:
                    # A lost response is not an ack. Do not replay it after a
                    # restart: it may already be visible in the chat.
                    delivery = "uncertain"
                    try:
                        (work / GATEWAY_UNCERTAIN).write_text("{}", encoding="utf-8")
                    except OSError:
                        logger.warning("Could not record uncertain council delivery: %s", work)
                else:
                    delivery = "not_shown"
                await self._record_context(origin, work, "result", delivery, summary, last)
            else:
                await card.finish_failed(last)
                delivery = _failure_delivery(card)
                try:
                    (work / GATEWAY_FAILED).write_text(
                        json.dumps({"delivery": delivery}), encoding="utf-8"
                    )
                except OSError:
                    logger.warning("Could not record failed council card: %s", work)
                await self._record_context(origin, work, "failure", delivery, None, last)
        except Exception:
            logger.warning("Council card for %s failed", work.name, exc_info=True)

    async def run(self, interval_s: float = 2.0) -> None:
        while True:
            try:
                self.scan_once()
            except Exception:
                logger.debug("Council run scan failed", exc_info=True)
            await asyncio.sleep(interval_s)
