"""Live replays against the REAL auxiliary client / Anthropic API.

Excluded from the default run (``-m 'not integration'``); run with
``pytest -m integration tests/agent/test_handoff_live_replay.py`` on a box with
a logged-in provider.  Pytest sandboxes CLOVER_HOME, so each case runs
``_live_handoff_probe.py`` in a child process on the real home.  They skip,
not fail, when the credentials or the saved notes of the field incident are
missing.

* title: the notes of deleg_25890b3b (a ~26k-char file; the old single 6 s call
  timed out on it 3 of 3 times) through the real ``router_backstop._prepare_handoff``:
  the card title must be a real one, inside the dispatch budget.
* voice line: ``voice_line`` on Opus 5.5 failed 4 of 4 with HTTP 400
  (``thinking.type.disabled``); it must now return the model's own line.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent import handoff_title as ht

pytestmark = pytest.mark.integration

PROBE = Path(__file__).with_name("_live_handoff_probe.py")
NOTES = Path.home() / ".clover" / "cache" / "handoff" / "deleg_25890b3b.md"
DISPATCH_BUDGET_SECONDS = 6.0


def _probe(kind: str, *args: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLOVER_TEST", "PYTEST"))}
    env["CLOVER_HOME"] = str(Path.home() / ".clover")
    env["HOME"] = str(Path.home())
    run = subprocess.run([sys.executable, str(PROBE), kind, *args], capture_output=True, text=True,
                         env=env, cwd=str(Path.home()), timeout=120)
    for line in run.stdout.splitlines():
        if line.startswith("PROBE "):
            return json.loads(line[6:])
    pytest.fail(f"probe {kind} gave no result:\n{run.stdout[-800:]}\n{run.stderr[-800:]}")


@pytest.fixture(autouse=True)
def _need_notes():
    if not NOTES.is_file():
        pytest.skip("the saved notes of deleg_25890b3b are not on this box")


def test_the_real_notes_file_gets_a_real_title_inside_the_dispatch_budget():
    out = _probe("title")
    print("\ntitle probe:", out)
    assert out["model_title"], "the small model answered inside its deadline"
    assert out["card_title"] != ht.FALLBACK_TITLE
    assert out["seconds"] < DISPATCH_BUDGET_SECONDS
    assert out["notes_written"]


def test_without_the_model_the_chain_still_names_the_offered_work():
    out = _probe("nomodel")
    print("\nno-model probe:", out)
    assert out["card_title"] != ht.FALLBACK_TITLE


@pytest.mark.parametrize("provider,model", [
    ("openai-codex", "gpt-6.1-sol-900k"), ("anthropic", "claude-opus-5-5")])
def test_a_dispatch_without_a_handoff_gets_a_voice_line_that_answers_the_question(
        provider, model, request):
    """Suni's live repro: tasks only, no handoff, no say, and a question beside
    the job.  The reply must be the model's own line answering "what model",
    never a canned "on it"."""
    out = _probe("nohandoff", provider, model)
    print(f"\nno-handoff opener ({model}):", out)
    assert not out["canned"], "the agent's own line, not a canned one"
    assert out["opener"] == out["voice_line"]
    low = out["opener"].lower()
    assert any(w in low for w in ("gpt", "opus", "claude", "sol", "model")), \
        "it answered which model it is on"


def test_the_voice_line_succeeds_on_opus_5_5():
    out = _probe("voice")
    print("\nvoice probe:", out)
    assert not out["is_fallback"], "the model wrote the line (no HTTP 400 on thinking.type.disabled)"
