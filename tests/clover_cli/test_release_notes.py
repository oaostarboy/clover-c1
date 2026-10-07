"""Behavior contract: the post-/update message names the version and what's new.

Covers the parser, version-range selection, the already-up-to-date case, missing
notes, truncation, the shipped RELEASE_NOTES.md invariants, and the three places
the gateway renders a SUCCESSFUL update (streaming watcher, legacy notifier,
updater-died conclusion) -- including that failed updates keep their messages
and that an earlier run's receipt is never reused.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from clover_cli import release_notes as rn

NOTES = """\
<!-- header comment with ## 9.9.9 | Not A Release | never -->

## 1.2.0 | Clover C1.2 | 2026-11-01
- Newest thing one.
- Newest thing two.

## 1.1.1 | Clover C1.1.1 | 2026-10-20
- Fix A.
- Fix B.
- Fix C.

## 1.1.0 | Clover C1.1 | 2026-10-07
- First named release.

## 1.0.0 | Clover C1 | 2026-08-23
- Day one.
"""


def entries(text: str = NOTES):
    return rn.parse_release_notes(text)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_reads_version_name_date_bullets_and_ignores_comments():
    parsed = entries()
    assert [e.version for e in parsed] == ["1.2.0", "1.1.1", "1.1.0", "1.0.0"]
    first = parsed[1]
    assert (first.name, first.date) == ("Clover C1.1.1", "2026-10-20")
    assert first.bullets == ("Fix A.", "Fix B.", "Fix C.")
    assert all(e.version != "9.9.9" for e in parsed)  # header comment is not a section


def test_parse_marks_draft_and_todo_entries_and_never_exposes_their_bullets():
    parsed = entries(
        "## 2.0.0 | Clover C2 | TBD\n<!-- draft -->\n- Secret plan.\n\n"
        "## 1.9.0 | Clover C1.9 | 2026-01-01\n- TODO fill me in\n"
    )
    assert [e.draft for e in parsed] == [True, True]
    section = rn.format_update_section(
        entries=parsed, to_version="2.0.0", from_version="1.8.0"
    )
    assert "Secret plan" not in section and "TODO" not in section
    assert "Clover C2 (v2.0.0)" in section  # name/version still shown


def test_parse_skips_malformed_and_duplicate_headers_and_cleans_text():
    parsed = entries(
        "## not-a-version | X | y\n- orphan\n\n"
        "## 1.0.0 | First | 2026-01-01\n- keep\u0007 me\n\n"
        "## v1.0.0 | Duplicate | 2026-01-02\n- dropped\n\n"
        "## 1.0 | Same as 1.0.0 | 2026-01-03\n- dropped too\n"
    )
    assert [(e.version, e.name) for e in parsed] == [("1.0.0", "First")]
    assert parsed[0].bullets == ("keep me",)


@pytest.mark.parametrize("junk", ["", "no sections here", "## \n- x", "\x00\x01\x02", None])
def test_parse_never_raises_on_junk(junk):
    assert isinstance(rn.parse_release_notes(junk), list)


def test_long_bullets_are_capped():
    parsed = entries("## 1.0.0 | N | d\n- " + "w" * 500 + "\n")
    assert len(parsed[0].bullets[0]) <= rn.MAX_BULLET_CHARS


@pytest.mark.parametrize(
    "a,b,expected",
    [("1.1.1", "1.1.0", 1), ("1.1", "1.1.0", 0), ("v1.10.0", "1.9.9", 1), ("1.0", "1.0.1", -1)],
)
def test_version_ordering_is_numeric_not_lexical(a, b, expected):
    pa, pb = rn.parse_version(a), rn.parse_version(b)
    assert (pa > pb) - (pa < pb) == expected


def test_load_missing_or_unreadable_file_is_empty(tmp_path):
    assert rn.load_release_notes(tmp_path / "nope.md") == []
    assert rn.load_release_notes(tmp_path) == []  # a directory


# ---------------------------------------------------------------------------
# Shipped RELEASE_NOTES.md invariants (relations, not frozen values)
# ---------------------------------------------------------------------------


def test_shipped_notes_cover_current_version_and_are_well_formed():
    from clover_cli import __version__

    shipped = rn.load_release_notes()
    assert shipped, "RELEASE_NOTES.md missing or unparseable"
    current = rn.find_entry(shipped, __version__)
    assert current is not None, f"RELEASE_NOTES.md has no entry for {__version__}"
    assert not current.draft, "the entry for the version being shipped is still a draft"
    assert current.name and current.date

    keys = [rn.parse_version(e.version) for e in shipped]
    assert keys == sorted(keys, reverse=True), "entries must be newest first"
    for e in shipped:
        if not e.draft:
            assert 1 <= len(e.bullets) <= 6, f"{e.version}: use 3-6 short bullets"
    # Any single release fits the chat budget on its own.
    for e in shipped:
        text = rn.format_update_section(
            entries=shipped, to_version=e.version, from_version="0.0.1"
        )
        assert len(text.splitlines()) <= rn.MAX_SECTION_LINES


# ---------------------------------------------------------------------------
# Range selection / rendering
# ---------------------------------------------------------------------------


def test_every_version_between_old_and_new_is_listed_newest_first_and_old_excluded():
    section = rn.format_update_section(
        entries=entries(), to_version="1.2.0", from_version="1.0.0",
        from_sha="a", to_sha="b", max_lines=30,  # selection test: no truncation
    )
    assert section.splitlines()[0] == "Now on Clover C1.2 (v1.2.0), updated from v1.0.0."
    assert "Newest thing one." in section
    assert "Fix A." in section and "First named release." in section
    assert "Day one." not in section  # the version the user already had
    assert section.index("Newest thing one.") < section.index("Fix A.") < section.index("First named release.")
    for name in ("Clover C1.2 (v1.2.0):", "Clover C1.1.1 (v1.1.1):", "Clover C1.1 (v1.1.0):"):
        assert name in section


def test_default_budget_truncates_the_same_range_to_the_chat_limit():
    section = rn.format_update_section(
        entries=entries(), to_version="1.2.0", from_version="1.0.0", from_sha="a", to_sha="b"
    )
    lines = section.splitlines()
    assert len(lines) <= rn.MAX_SECTION_LINES
    assert lines[-1] == "…and 1 more"  # the oldest bullet is the one dropped
    assert "Newest thing one." in section and "First named release." not in section


def test_single_version_update_has_no_per_version_heading():
    section = rn.format_update_section(
        entries=entries(), to_version="1.1.1", from_version="1.1.0", from_sha="a", to_sha="b"
    )
    assert section.splitlines() == [
        "Now on Clover C1.1.1 (v1.1.1), updated from v1.1.0.",
        "What's new:",
        "• Fix A.",
        "• Fix B.",
        "• Fix C.",
    ]


def test_versions_newer_than_the_running_one_are_not_shown():
    section = rn.format_update_section(
        entries=entries(), to_version="1.1.0", from_version="1.0.0", from_sha="a", to_sha="b"
    )
    assert "Newest thing" not in section and "Fix A." not in section


def test_already_up_to_date_does_not_repeat_notes():
    section = rn.format_update_section(
        entries=entries(), to_version="1.1.1", from_version="1.1.1", from_sha="same", to_sha="same"
    )
    assert section == "Already up to date. You are on Clover C1.1.1 (v1.1.1)."
    assert "What's new" not in section and "Fix A." not in section


def test_downloaded_but_not_running_new_code_says_restart_instead_of_notes():
    section = rn.format_update_section(
        entries=entries(), to_version="1.1.0", from_version="1.1.0",
        from_sha="old", to_sha="old", pulled_new_code=True,
    )
    assert "/restart" in section and "What's new" not in section


def test_missing_notes_still_shows_name_and_version_without_failing():
    # Version known, but no entries at all.
    none = rn.format_update_section(
        entries=[], to_version="1.3.0", from_version="1.1.0", from_sha="a", to_sha="b"
    )
    assert none == "Now on Clover v1.3.0, updated from v1.1.0."
    # Entry exists for the new version but none of the in-between ones have bullets.
    only_name = rn.format_update_section(
        entries=entries("## 1.3.0 | Clover C1.3 | 2026-12-01\n"),
        to_version="1.3.0", from_version="1.1.0", from_sha="a", to_sha="b",
    )
    assert only_name == "Now on Clover C1.3 (v1.3.0), updated from v1.1.0."


def test_unknown_starting_version_shows_name_only_not_every_note():
    section = rn.format_update_section(
        entries=entries(), to_version="1.1.1", from_version=None, from_sha=None, to_sha="b"
    )
    assert section == "Now on Clover C1.1.1 (v1.1.1)."


def test_no_running_version_renders_nothing():
    assert rn.format_update_section(entries=entries(), to_version="") == ""


def test_long_lists_truncate_with_and_n_more_within_the_line_budget():
    many = "## 2.0.0 | Clover C2 | d\n" + "".join(f"- Point {i}.\n" for i in range(6))
    many += "## 1.5.0 | Clover C1.5 | d\n" + "".join(f"- Older {i}.\n" for i in range(6))
    section = rn.format_update_section(
        entries=entries(many), to_version="2.0.0", from_version="1.0.0", from_sha="a", to_sha="b"
    )
    lines = section.splitlines()
    assert len(lines) <= rn.MAX_SECTION_LINES
    assert lines[-1].startswith("…and ") and lines[-1].endswith(" more")
    shown = sum(1 for line in lines if line.startswith("• "))
    hidden = int(lines[-1].split()[1])
    assert shown + hidden == 12  # nothing silently lost
    assert not lines[-2].endswith(":")  # no dangling per-version heading


def test_truncation_never_leaves_a_heading_as_the_last_kept_line():
    text = ""
    for i in range(1, 6):
        text += f"## 1.{i}.0 | Clover C1.{i} | d\n- one {i}\n- two {i}\n"
    section = rn.format_update_section(
        entries=entries(text), to_version="1.5.0", from_version="1.0.0", from_sha="a", to_sha="b"
    )
    lines = section.splitlines()
    assert len(lines) <= rn.MAX_SECTION_LINES
    body = lines[2:-1] if lines[-1].startswith("…and") else lines[2:]
    assert body and not body[-1].endswith(":")


# ---------------------------------------------------------------------------
# Which receipt counts
# ---------------------------------------------------------------------------


def _write_receipt(home: Path, *, started: datetime, pre_v, pre_sha, post_sha):
    d = home / "logs" / "update_receipts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(
        json.dumps(
            {
                "started_at": started.isoformat(),
                "outcome": "success",
                "pre_update": {"version": pre_v, "sha": pre_sha},
                "post_update": {"version": "1.1.1", "sha": post_sha},
            }
        ),
        encoding="utf-8",
    )


def _marker(home: Path, **fields) -> Path:
    p = home / ".update_pending.json"
    p.write_text(json.dumps({"platform": "telegram", "chat_id": "1", **fields}), encoding="utf-8")
    return p


@pytest.fixture()
def notes_file(tmp_path, monkeypatch):
    p = tmp_path / "RELEASE_NOTES.md"
    p.write_text(NOTES, encoding="utf-8")
    monkeypatch.setattr(rn, "default_notes_path", lambda: p)
    return p


def test_this_runs_receipt_supplies_the_from_version_for_an_old_gateways_update(tmp_path, notes_file):
    """First hop: the OLD gateway wrote a marker without from_version; the
    receipt its updater wrote is the only record of where the user started."""
    marker = _marker(tmp_path)
    now = datetime.now(timezone.utc)
    _write_receipt(tmp_path, started=now + timedelta(seconds=2), pre_v="1.1.0", pre_sha="old", post_sha="new")
    pending, mtime = rn.read_marker([marker])
    text = rn.build_post_update_section(
        tmp_path, pending, mtime, notes_path=notes_file,
        running_version="1.1.1", running_sha="new",
    )
    assert text.splitlines()[0] == "Now on Clover C1.1.1 (v1.1.1), updated from v1.1.0."
    assert "• Fix A." in text


def test_an_earlier_updates_receipt_is_never_reused(tmp_path, notes_file):
    marker = _marker(tmp_path)
    long_ago = datetime.now(timezone.utc) - timedelta(days=3)
    _write_receipt(tmp_path, started=long_ago, pre_v="1.0.0", pre_sha="ancient", post_sha="new")
    pending, mtime = rn.read_marker([marker])
    assert rn.read_this_runs_receipt(tmp_path, mtime) is None
    text = rn.build_post_update_section(
        tmp_path, pending, mtime, notes_path=notes_file,
        running_version="1.1.1", running_sha="new",
    )
    assert text == "Now on Clover C1.1.1 (v1.1.1)."  # no stale "updated from v1.0.0" / notes


def test_no_marker_means_no_receipt_is_trusted(tmp_path):
    _write_receipt(tmp_path, started=datetime.now(timezone.utc), pre_v="1.0.0", pre_sha="a", post_sha="b")
    assert rn.read_this_runs_receipt(tmp_path, None) is None
    assert rn.read_this_runs_receipt(tmp_path, 0.0) is None


def test_marker_recorded_by_a_new_gateway_wins_over_the_receipt(tmp_path, notes_file):
    marker = _marker(tmp_path, from_version="1.1.0", from_sha="old")
    _write_receipt(
        tmp_path, started=datetime.now(timezone.utc) + timedelta(seconds=1),
        pre_v="1.1.1", pre_sha="new", post_sha="new",  # earlier pull, never restarted
    )
    pending, mtime = rn.read_marker([marker])
    text = rn.build_post_update_section(
        tmp_path, pending, mtime, notes_path=notes_file,
        running_version="1.1.1", running_sha="new",
    )
    assert "updated from v1.1.0" in text and "• Fix A." in text


def test_already_current_run_reports_up_to_date_from_marker_alone(tmp_path, notes_file):
    marker = _marker(tmp_path, from_version="1.1.1", from_sha="same")
    pending, mtime = rn.read_marker([marker])
    text = rn.build_post_update_section(
        tmp_path, pending, mtime, notes_path=notes_file,
        running_version="1.1.1", running_sha="same",
    )
    assert text == "Already up to date. You are on Clover C1.1.1 (v1.1.1)."


def test_corrupt_inputs_degrade_to_empty_text_not_exceptions(tmp_path):
    (tmp_path / ".update_pending.json").write_text("{not json", encoding="utf-8")
    # Unreadable marker: still no exception; with a known running version the
    # result is just the name/version line.
    out = rn.build_post_update_section(
        tmp_path, None, None, running_version="1.1.1", running_sha="x", notes_path=tmp_path / "missing.md"
    )
    assert out == "Now on Clover v1.1.1."
    assert rn.section_for_update_markers(tmp_path, [tmp_path / ".update_pending.json"]) != "boom"
    assert rn.build_post_update_section(tmp_path, {"from_version": object()}, 1.0, running_version="1.1.1") != "boom"


def test_pending_marker_origin_records_running_version_and_sha():
    from clover_cli import __version__

    origin = rn.pending_marker_origin()
    assert origin.get("from_version") == __version__


# ---------------------------------------------------------------------------
# Gateway: the three places a successful /update is reported
# ---------------------------------------------------------------------------


def _runner():
    from gateway.run import GatewayRunner

    r = object.__new__(GatewayRunner)
    r.adapters = {}
    r._voice_mode = {}
    r._update_prompt_pending = {}
    r._running_agents = {}
    r._running_agents_ts = {}
    r._pending_messages = {}
    r._pending_approvals = {}
    r._failed_platforms = {}
    r.config = None
    r._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": False}}
    return r


@pytest.fixture()
def home(tmp_path, notes_file, monkeypatch):
    h = tmp_path / "clover"
    h.mkdir()
    # The restarted gateway runs the NEW code: v1.1.1 at sha "new".
    monkeypatch.setattr(rn, "current_code_identity", lambda: ("1.1.1", "new"))
    with patch("gateway.run._clover_home", h):
        yield h


def _sent_text(adapter) -> str:
    return "\n".join(str(c.args[1]) for c in adapter.send.call_args_list)


@pytest.mark.asyncio
async def test_legacy_notifier_success_message_has_name_version_and_notes_above_output(home):
    runner = _runner()
    adapter = AsyncMock()
    runner.adapters = {__import__("gateway.config", fromlist=["Platform"]).Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.0", from_sha="old")
    (home / ".update_output.txt").write_text("✓ Code updated!", encoding="utf-8")
    (home / ".update_exit_code").write_text("0", encoding="utf-8")

    assert await runner._send_update_notification() is True

    msg = _sent_text(adapter)
    assert msg.startswith("✅ Clover update finished.")
    assert "Clover C1.1.1 (v1.1.1)" in msg and "updated from v1.1.0" in msg
    assert "• Fix A." in msg
    assert msg.index("• Fix A.") < msg.index("```")  # notes first, raw output last
    assert not (home / ".update_pending.json").exists()  # markers still cleaned up


@pytest.mark.asyncio
async def test_legacy_notifier_without_output_still_shows_version(home):
    from gateway.config import Platform

    runner = _runner()
    adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.1", from_sha="new")
    (home / ".update_exit_code").write_text("0", encoding="utf-8")

    await runner._send_update_notification()

    assert _sent_text(adapter) == (
        "✅ Clover update finished successfully.\n\n"
        "Already up to date. You are on Clover C1.1.1 (v1.1.1)."
    )


@pytest.mark.asyncio
async def test_failed_update_messages_are_unchanged_and_carry_no_notes(home):
    from gateway.config import Platform

    runner = _runner()
    adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.0", from_sha="old")
    (home / ".update_exit_code").write_text("1", encoding="utf-8")

    await runner._send_update_notification()

    assert _sent_text(adapter) == (
        "❌ Clover update failed. Check the gateway logs or run `clover update` manually for details."
    )


@pytest.mark.asyncio
async def test_streaming_watcher_final_message_has_version_and_notes_in_one_message(home):
    from gateway.config import Platform

    runner = _runner()
    adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.0", from_sha="old", session_key="agent:main:telegram:dm:1")
    (home / ".update_output.txt").write_text("→ Fetching updates...\n", encoding="utf-8")
    (home / ".update_exit_code").write_text("0", encoding="utf-8")

    await runner._watch_update_progress(poll_interval=0.05, stream_interval=0.05, timeout=5.0)

    finals = [str(c.args[1]) for c in adapter.send.call_args_list if "update finished" in str(c.args[1]).lower()]
    assert len(finals) == 1, "exactly one completion message (no extra message)"
    assert "Clover C1.1.1 (v1.1.1)" in finals[0] and "• Fix B." in finals[0]


@pytest.mark.asyncio
async def test_streaming_watcher_failure_message_is_unchanged(home):
    from gateway.config import Platform

    runner = _runner()
    adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.0", from_sha="old", session_key="agent:main:telegram:dm:1")
    (home / ".update_exit_code").write_text("1", encoding="utf-8")

    await runner._watch_update_progress(poll_interval=0.05, stream_interval=0.05, timeout=5.0)

    assert "❌ Clover update failed (exit code 1)." in _sent_text(adapter)
    assert "Clover C1.1.1" not in _sent_text(adapter)


@pytest.mark.asyncio
async def test_updater_died_after_restart_new_gateway_adds_notes_for_the_first_hop(home):
    """The real chat /update: the updater is the old gateway's child and dies at
    restart; the NEW gateway concludes from this run's receipt. The old gateway
    never wrote from_version, so the receipt supplies it."""
    from gateway.config import Platform
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, adapter = make_restart_runner()
    marker = _marker(home)  # as written by a 1.1.0 gateway: no from_version
    _write_receipt(
        home, started=datetime.now(timezone.utc) + timedelta(seconds=1),
        pre_v="1.1.0", pre_sha="old", post_sha="new",
    )
    receipt = home / "logs" / "update_receipts" / "latest.json"
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["post_update"]["short_sha"] = "abcd1234"
    receipt.write_text(json.dumps(data), encoding="utf-8")
    paths = [home / n for n in (".update_pending.json", ".update_pending.claimed.json",
                                ".update_output.txt", ".update_exit_code", ".update_prompt.json")]

    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="42", session_key=None,
        metadata=None, platform=Platform.TELEGRAM,
    )

    assert len(adapter.sent) == 1
    msg = adapter.sent[0]
    assert msg.startswith("✅ Clover update finished.")
    assert "Now at abcd1234." in msg  # existing text preserved
    assert "Clover C1.1.1 (v1.1.1)" in msg and "updated from v1.1.0" in msg
    assert "• Fix A." in msg
    assert not marker.exists()


@pytest.mark.asyncio
async def test_updater_died_with_only_an_old_receipt_does_not_claim_success_or_notes(home):
    from gateway.config import Platform
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, adapter = make_restart_runner()
    _marker(home)
    _write_receipt(home, started=datetime.now(timezone.utc) - timedelta(days=2),
                   pre_v="1.0.0", pre_sha="a", post_sha="b")
    paths = [home / n for n in (".update_pending.json", ".update_pending.claimed.json",
                                ".update_output.txt", ".update_exit_code", ".update_prompt.json")]
    # Receipt is older than the marker -> still the honest "no result" notice.
    old = time.time() - 3 * 86400
    os.utime(home / "logs" / "update_receipts" / "latest.json", (old, old))

    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="42", session_key=None,
        metadata=None, platform=Platform.TELEGRAM,
    )

    assert "without reporting a result" in adapter.sent[0]
    assert "What's new" not in adapter.sent[0] and "Fix A." not in adapter.sent[0]


@pytest.mark.asyncio
async def test_slash_update_records_the_running_version_in_the_pending_marker(tmp_path):
    from clover_cli import __version__
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    runner = _runner()
    event = MessageEvent(
        text="/update",
        source=SessionSource(platform=Platform.TELEGRAM, user_id="1", chat_id="2", user_name="u"),
    )
    h = tmp_path / "clover"
    h.mkdir()
    fake_root = tmp_path / "project"
    (fake_root / ".git").mkdir(parents=True)
    (fake_root / "gateway").mkdir()
    (fake_root / "gateway" / "run.py").touch()
    with patch("gateway.run._clover_home", h), \
         patch("gateway.run.__file__", str(fake_root / "gateway" / "run.py")), \
         patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), \
         patch("clover_cli.update_contract.escape_gateway_cgroup", side_effect=lambda argv, env: (argv, env)), \
         patch("subprocess.Popen"):
        reply = await runner._handle_update_command(event)

    assert (h / '.update_pending.json').exists(), reply
    pending = json.loads((h / ".update_pending.json").read_text(encoding="utf-8"))
    assert pending["from_version"] == __version__


def test_receipt_only_origin_never_claims_already_up_to_date(tmp_path, notes_file):
    """A re-exec'd updater child (Windows shim hand-off) writes a receipt whose
    "before" is already the new code; equal shas there prove nothing."""
    marker = _marker(tmp_path)  # old gateway: no from_* fields
    _write_receipt(
        tmp_path, started=datetime.now(timezone.utc) + timedelta(seconds=1),
        pre_v="1.1.1", pre_sha="new", post_sha="new",
    )
    pending, mtime = rn.read_marker([marker])
    text = rn.build_post_update_section(
        tmp_path, pending, mtime, notes_path=notes_file,
        running_version="1.1.1", running_sha="new",
    )
    assert text == "You are on Clover C1.1.1 (v1.1.1)."
    assert "Already up to date" not in text
