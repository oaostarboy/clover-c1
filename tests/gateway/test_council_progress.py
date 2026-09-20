"""Compact Telegram UI contract for multi-model council runs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.council_progress import (
    format_council_result,
    parse_council_args,
    render_council_card,
)
from gateway.slash_commands import GatewaySlashCommandsMixin
from clover_cli.commands import resolve_command


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = (
    ROOT
    / "optional-skills"
    / "autonomous-ai-agents"
    / "council"
    / "scripts"
    / "council_run.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("council_run_progress", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_defaults_to_full_without_rewriting_question():
    assert parse_council_args("Should we launch Friday?") == (
        "full",
        "Should we launch Friday?",
    )


@pytest.mark.parametrize("mode", ["quick", "full", "deep"])
def test_parse_accepts_explicit_mode(mode):
    assert parse_council_args(f"{mode} Keep the wording exactly?") == (
        mode,
        "Keep the wording exactly?",
    )


def test_parse_requires_a_question():
    with pytest.raises(ValueError, match="Usage: /council"):
        parse_council_args("deep")


def test_council_is_a_first_class_gateway_command():
    command = resolve_command("council")
    assert command is not None
    assert command.gateway_only is True
    assert command.args_hint == "[quick|full|deep] <question>"
    assert command.busy_policy == "reject"


def test_live_card_is_distinct_and_compact():
    card = render_council_card(
        {
            "status": "running",
            "mode": "full",
            "stage": "arguments",
            "seat_done": 3,
            "seat_total": 5,
            "review_done": 0,
            "review_total": 5,
            "elapsed_s": 24,
        }
    )
    assert card == (
        "🏛 Council · Full\n"
        "◉ Opening arguments 3/5\n"
        "○ Cross-review\n"
        "○ Chairman"
    )
    assert "tool" not in card.lower()
    assert len(card.splitlines()) == 4


def test_deep_card_shows_distinct_attack_stage_without_bloat():
    card = render_council_card(
        {
            "status": "running",
            "mode": "deep",
            "stage": "attack",
            "seat_done": 6,
            "seat_total": 6,
            "review_done": 6,
            "review_total": 6,
            "elapsed_s": 91,
        }
    )
    assert "✓ Opening arguments 6/6" in card
    assert "✓ Cross-review 6/6" in card
    assert "◉ Verdict attack" in card
    assert len(card.splitlines()) <= 6


def test_done_card_collapses_like_activity_summary_but_stays_council_specific():
    card = render_council_card(
        {
            "status": "done",
            "mode": "full",
            "seat_done": 5,
            "seat_total": 5,
            "review_done": 5,
            "review_total": 5,
            "elapsed_s": 102,
        }
    )
    assert card == "🏛 Council · 5 seats · 5 reviews · 1m42s"


def test_failed_card_keeps_failed_stage_as_breadcrumb():
    card = render_council_card(
        {
            "status": "failed",
            "mode": "deep",
            "stage": "chairman",
            "elapsed_s": 75,
        }
    )
    assert card == "🏛 Council failed · Chairman · 1m15s"


def test_final_reply_keeps_verdict_out_of_compact_card():
    reply = format_council_result(
        {
            "mode": "full",
            "verdict": "Ship the small version.",
            "next": "Run one pilot.",
            "dissent": "The pilot may understate scale risk.",
            "stalled": [],
        }
    )
    assert reply == (
        "🏛 **Council verdict**\n\n"
        "**VERDICT:** Ship the small version.\n"
        "**NEXT:** Run one pilot.\n"
        "**DISSENT:** The pilot may understate scale risk.\n\n"
        "Mode: full · all seats returned"
    )


def test_runner_progress_file_is_atomic_and_machine_readable(tmp_path):
    runner = _load_runner()
    runner.write_progress(
        tmp_path,
        status="running",
        mode="full",
        stage="cross_review",
        seat_done=5,
        seat_total=5,
        review_done=2,
        review_total=5,
        started_at=100.0,
        now=130.0,
    )
    payload = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert payload["stage"] == "cross_review"
    assert payload["review_done"] == 2
    assert payload["elapsed_s"] == 30
    assert not (tmp_path / "progress.json.tmp").exists()


def test_collect_reports_each_landed_seat(tmp_path):
    runner = _load_runner()
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("GIST: first", encoding="utf-8")
    second.write_text("GIST: second", encoding="utf-8")
    progress = []

    answers, stalled = runner._collect(
        {"A": first, "B": second},
        timeout_s=0.2,
        poll_s=0.01,
        settle_s=0,
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert set(answers) == {"A", "B"}
    assert stalled == []
    assert progress[-1] == (2, 2)


@pytest.mark.asyncio
async def test_gateway_edits_one_council_card_then_returns_verdict(tmp_path):
    script = (
        tmp_path
        / "skills"
        / "autonomous-ai-agents"
        / "council"
        / "scripts"
        / "council_run.py"
    )
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        "args=sys.argv[1:]\n"
        "run_id=args[args.index('--id')+1]\n"
        "mode=args[args.index('--mode')+1]\n"
        "work=pathlib.Path(os.environ['CLOVER_HOME'])/'council'/'runs'/run_id\n"
        "work.mkdir(parents=True)\n"
        "def put(stage,status='running',done=0):\n"
        " p={'status':status,'mode':mode,'stage':stage,'seat_done':done,'seat_total':3,'review_done':0,'review_total':0,'elapsed_s':done}\n"
        " (work/'progress.json').write_text(json.dumps(p))\n"
        "put('arguments',done=1)\n"
        "time.sleep(1.1)\n"
        "put('chairman',done=3)\n"
        "time.sleep(1.1)\n"
        "summary={'mode':mode,'verdict':'Ship it.','next':'Run a pilot.','dissent':'Scale risk.','stalled':[]}\n"
        "(work/'summary.json').write_text(json.dumps(summary))\n"
        "put('chairman','done',3)\n",
        encoding="utf-8",
    )

    class Adapter:
        def __init__(self):
            self.updates = []

        async def send_or_update_status(self, chat_id, status_key, content, metadata=None):
            self.updates.append((chat_id, status_key, content, metadata))

    adapter = Adapter()

    class Runner(GatewaySlashCommandsMixin):
        def _resolve_profile_home_for_source(self, source):
            return tmp_path

        def _thread_metadata_for_source(self, source):
            return {"thread_id": "7"}

        def _adapter_for_source(self, source):
            return adapter

    source = SimpleNamespace(chat_id="123")
    event = SimpleNamespace(source=source, get_command_args=lambda: "quick Is this clean?")

    reply = await Runner()._handle_council_command(event)

    assert reply.startswith("🏛 **Council verdict**")
    assert "**VERDICT:** Ship it." in reply
    assert len({update[1] for update in adapter.updates}) == 1
    assert adapter.updates[0][2].startswith("🏛 Council · Quick")
    assert any("◉ Chairman" in update[2] for update in adapter.updates)
    assert adapter.updates[-1][2] == "🏛 Council · 3 seats · 3s"
