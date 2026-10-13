"""Out-of-pytest probe for ``test_handoff_live_replay``: pytest sandboxes
CLOVER_HOME (no credentials), so the real-model calls run here, in a child
process on the real home.  Prints one JSON object.

    python tests/agent/_live_handoff_probe.py title|nomodel|voice
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

NOTES = Path.home() / ".clover" / "cache" / "handoff" / "deleg_25890b3b.md"
MESSAGE = "1. ok, do it.\n\n2. ok.\n\n3. ye.\n\nYes."


def _inputs():
    from agent import handoff_notes as hn

    notes = NOTES.read_text(encoding="utf-8")
    conversation = notes.split("## Recent conversation before this request (oldest first)")[1]
    conversation = conversation.split("## This turn's work so far")[0]
    history = [(m.group(1), m.group(2).strip()) for m in re.finditer(
        r"\*\*(user|assistant):\*\* (.*?)(?=\n\*\*(?:user|assistant):\*\*|\Z)", conversation, re.S)]
    log = hn.WorkLog()
    for m in re.finditer(r"### \d+\. (\w+)\((.*?)\)\n\n(.*?)(?=\n### \d+\.|\n## |\Z)",
                         notes.split("## This turn's work so far")[1], re.S):
        log.add(m.group(1), m.group(2), m.group(3))
    return history, log


def title(use_model: bool = True) -> dict:
    from agent import delegation_router as dr
    from agent import handoff_notes as hn
    from agent import router_backstop as rb

    history, log = _inputs()
    tmp = Path(tempfile.mkdtemp())
    hn.notes_dir = lambda: tmp  # the notes go to a throwaway dir
    if not use_model:
        hn.handoff_title = lambda *a, **k: hn.Brief()
    agent = SimpleNamespace(session_id="live-replay", _router_completion_ids=(), _todo_store=None)
    verdict = dr.Verdict(mode="direct", tasks=(), reason="")
    state = rb.RouterTurn(verdict=verdict, user_message=MESSAGE, started_at=time.monotonic(),
                          settings=dr.RouterSettings(enabled=True, direct_backstop_calls=3))
    state.work_log, state.history = log, history
    started = time.monotonic()
    did, brief = rb._prepare_handoff(agent, state)
    took = time.monotonic() - started
    card = rb._runtime_handoff(verdict, MESSAGE, agent=agent, history=history)
    return {"seconds": round(took, 2), "model_title": brief.title, "model_goal": brief.goal,
            "card_title": brief.title or card["work"], "fallback_card_title": card["work"],
            "notes_written": bool(did) and (tmp / f"{did}.md").is_file()}


def voice() -> dict:
    from agent import handoff_notes as hn
    from run_agent import AIAgent

    agent = AIAgent(provider="anthropic", model="claude-opus-5-5", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, skip_background_review=True,
                    enabled_toolsets=["todo"], max_iterations=5, platform="telegram")
    messages = [
        {"role": "user", "content": "want to add the tracking to the site?"},
        {"role": "assistant", "content": "yes, i can add it. want me to start on the tracking?"},
        {"role": "user", "content": "ok, do it."},
    ]
    started = time.monotonic()
    line = hn.voice_line(agent, messages, "adding Google Ads quote tracking", message="ok, do it.")
    return {"seconds": round(time.monotonic() - started, 2), "line": line,
            "is_fallback": line == hn.FALLBACK_LINE}


if __name__ == "__main__":
    kind = sys.argv[1]
    out = {"title": lambda: title(True), "nomodel": lambda: title(False), "voice": voice}[kind]()
    print("PROBE " + json.dumps(out))
