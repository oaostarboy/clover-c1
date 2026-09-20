"""Compact council-specific progress cards for gateway surfaces."""

from __future__ import annotations

import re
from typing import Any, Mapping


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


def format_council_result(summary: Mapping[str, Any]) -> str:
    """Format the actual reply; the compact card deliberately omits the verdict."""
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
    return (
        "🏛 **Council answer**\n\n"
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
