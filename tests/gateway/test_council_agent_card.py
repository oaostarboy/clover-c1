"""Agent-launched councils get the same live 🏛 card as /council, once per run."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.council_progress import CouncilRunWatcher
from gateway.slash_commands import GatewaySlashCommandsMixin

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = (
    ROOT / "optional-skills" / "autonomous-ai-agents" / "council" / "scripts" / "council_run.py"
)
SESSION_ENV = {
    "CLOVER_SESSION_PLATFORM": "telegram",
    "CLOVER_SESSION_CHAT_ID": "123",
    "CLOVER_SESSION_THREAD_ID": "7",
    "CLOVER_SESSION_KEY": "agent:main:telegram:dm:123",
}


def _load_runner():
    spec = importlib.util.spec_from_file_location("council_run_agent_card", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Adapter:
    def __init__(self):
        self.updates = []
        self.sends = []

    async def send_or_update_status(self, chat_id, status_key, content, metadata=None):
        self.updates.append((chat_id, status_key, content, metadata))

    async def send(self, chat_id, content, metadata=None):
        self.sends.append((chat_id, content, metadata))


def _watcher(home: Path, adapter: Adapter) -> CouncilRunWatcher:
    return CouncilRunWatcher(
        homes=lambda: [home],
        resolve_target=lambda origin: (adapter, origin["chat_id"], {"thread_id": origin["thread_id"]}),
        poll_s=0.01,
        card_style=lambda origin: "classic",  # these tests pin the rollback card
    )


def _progress(runner, work, stage, status="running", seats=0):
    runner.write_progress(
        work,
        status=status,
        mode="full",
        stage=stage,
        seat_done=seats,
        seat_total=5,
        review_done=0,
        review_total=5,
        started_at=100.0,
        now=100.0 + seats,
    )


@pytest.mark.asyncio
async def test_agent_launched_run_gets_one_live_card_and_the_result(tmp_path, monkeypatch):
    for key, value in SESSION_ENV.items():
        monkeypatch.setenv(key, value)
    runner = _load_runner()
    work = tmp_path / "council" / "runs" / "council-agent-1"
    work.mkdir(parents=True)
    assert runner.write_origin(work) is True
    _progress(runner, work, "arguments", seats=1)

    adapter = Adapter()
    watcher = _watcher(tmp_path, adapter)
    (task,) = watcher.scan_once()
    assert watcher.scan_once() == []  # never a second card for the same run

    await asyncio.sleep(0.05)
    _progress(runner, work, "chairman", seats=5)
    await asyncio.sleep(0.05)
    (work / "summary.json").write_text(
        json.dumps(
            {"mode": "full", "verdict": "Ship it.", "why": "Pilot first.", "caveat": "Scale.", "stalled": []}
        ),
        encoding="utf-8",
    )
    _progress(runner, work, "chairman", status="done", seats=5)
    await asyncio.wait_for(task, 5)

    assert {u[1] for u in adapter.updates} == {"council:council-agent-1"}
    assert {u[0] for u in adapter.updates} == {"123"}
    assert adapter.updates[0][2].startswith("🏛 Council · Full")
    assert any("◉ Chairman" in u[2] for u in adapter.updates)
    assert adapter.updates[-1][2].startswith("🏛 Council · 5 seats")
    assert "◉" not in adapter.updates[-1][2]
    assert len(adapter.sends) == 1
    chat_id, reply, metadata = adapter.sends[0]
    assert chat_id == "123" and metadata == {"thread_id": "7"}
    assert reply.startswith("🏛 **Council answer**") and "• Ship it." in reply


@pytest.mark.asyncio
async def test_slash_council_run_is_not_duplicated_by_the_watcher(tmp_path):
    script = tmp_path / "skills" / "autonomous-ai-agents" / "council" / "scripts" / "council_run.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        "args=sys.argv[1:]\n"
        "run_id=args[args.index('--id')+1]\n"
        "work=pathlib.Path(os.environ['CLOVER_HOME'])/'council'/'runs'/run_id\n"
        "work.mkdir(parents=True)\n"
        "(work/'origin.json').write_text(json.dumps({'platform':'telegram','chat_id':'123','thread_id':'7','created_at':time.time(),'pid':os.getpid()}))\n"
        "def put(stage,status='running',done=0):\n"
        " (work/'progress.json').write_text(json.dumps({'status':status,'mode':'quick','stage':stage,'seat_done':done,'seat_total':3,'review_done':0,'review_total':0,'elapsed_s':done}))\n"
        "put('arguments',done=1)\n"
        "time.sleep(1.3)\n"
        "(work/'summary.json').write_text(json.dumps({'mode':'quick','verdict':'Ship it.','next':'Pilot.','dissent':'Scale.','stalled':[]}))\n"
        "put('chairman','done',3)\n",
        encoding="utf-8",
    )
    adapter = Adapter()

    class Runner(GatewaySlashCommandsMixin):
        def _council_card_style(self, platform):
            return "classic"

        def _resolve_profile_home_for_source(self, source):
            return tmp_path

        def _thread_metadata_for_source(self, source):
            return {"thread_id": "7"}

        def _adapter_for_source(self, source):
            return adapter

    watcher = _watcher(tmp_path, adapter)
    watching = asyncio.create_task(watcher.run(interval_s=0.05))
    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="123"), get_command_args=lambda: "quick Is this clean?"
    )
    try:
        reply = await Runner()._handle_council_command(event)
        await asyncio.sleep(0.2)
    finally:
        watching.cancel()

    assert reply.startswith("🏛 **Council answer**")
    assert len({u[1] for u in adapter.updates}) == 1
    assert adapter.sends == []  # the handler returns the result; the watcher stays out
    assert not watcher.tasks


@pytest.mark.asyncio
async def test_cli_run_without_origin_gets_no_card(tmp_path, monkeypatch):
    for key in SESSION_ENV:
        monkeypatch.delenv(key, raising=False)
    runner = _load_runner()
    work = tmp_path / "council" / "runs" / "council-cli-1"
    work.mkdir(parents=True)
    assert runner.write_origin(work) is False
    assert not (work / "origin.json").exists()
    _progress(runner, work, "arguments", seats=1)

    adapter = Adapter()
    assert _watcher(tmp_path, adapter).scan_once() == []
    assert adapter.updates == [] and adapter.sends == []


@pytest.mark.asyncio
async def test_run_finished_before_the_gateway_saw_it_is_not_replayed(tmp_path, monkeypatch):
    for key, value in SESSION_ENV.items():
        monkeypatch.setenv(key, value)
    runner = _load_runner()
    work = tmp_path / "council" / "runs" / "council-old"
    work.mkdir(parents=True)
    runner.write_origin(work)
    _progress(runner, work, "chairman", status="done", seats=5)
    adapter = Adapter()
    assert _watcher(tmp_path, adapter).scan_once() == []
    assert adapter.updates == [] and adapter.sends == []


def test_gateway_routes_agent_run_through_the_launching_session(tmp_path):
    adapter = object()
    source = SimpleNamespace(chat_id="123", thread_id="7")

    class Runner(GatewaySlashCommandsMixin):
        session_store = SimpleNamespace(
            _entries={"agent:main:telegram:dm:123": SimpleNamespace(origin=source)}
        )

        def _adapter_for_source(self, src):
            assert src is source
            return adapter

        def _thread_metadata_for_source(self, src):
            return {"thread_id": src.thread_id}

    target = Runner()._council_run_target(
        {"platform": "telegram", "chat_id": "123", "session_key": "agent:main:telegram:dm:123"}
    )
    assert target == (adapter, "123", {"thread_id": "7"})
