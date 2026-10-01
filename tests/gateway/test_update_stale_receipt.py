"""A dead updater's chat report must come from THIS run's receipt, never a stale one.

When ``/update`` runs from a chat and the updater dies before writing
``.update_exit_code``, the gateway concludes from
``logs/update_receipts/latest.json`` (``_conclude_update_after_updater_death``).
It trusts a receipt only if it started after this run's pending marker.

Defect (review F5, 62ef460b): when neither pending marker could be read,
``pending_mtime`` stayed 0.0 and ``recent = pending_mtime == 0.0 or ...`` made
EVERY receipt count as this run. A ``success`` receipt left by an earlier
update then produced "✅ Clover update finished ... Now at <old sha>" for an
update that never ran: exactly the incident shape, where the updater is killed
before it writes its own receipt.

Contract: no readable marker means no proof this receipt belongs to this run,
so the report is the honest "ended without reporting a result" notice.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from tests.gateway.restart_test_helpers import make_restart_runner


def _write_receipt(home, **fields):
    receipts = home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "latest.json").write_text(json.dumps(fields), encoding="utf-8")


async def _conclude(runner, adapter, home):
    paths = [home / n for n in (".update_pending.json", ".update_pending.claimed.json",
                                ".update_output.txt", ".update_exit_code",
                                ".update_prompt.json")]
    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="42", session_key=None,
        metadata=None, platform=Platform.TELEGRAM,
    )


@pytest.mark.asyncio
async def test_stale_success_receipt_without_markers_is_not_reported_as_success(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    # A success receipt from an EARLIER update; no pending marker on disk.
    _write_receipt(tmp_path, outcome="success",
                   started_at="2026-09-01T00:00:00+00:00",
                   post_update={"short_sha": "0ldc0ffee"})
    runner, adapter = make_restart_runner()

    await _conclude(runner, adapter, tmp_path)

    assert "finished" not in adapter.sent[0]
    assert "0ldc0ffee" not in adapter.sent[0]
    assert "without reporting a result" in adapter.sent[0]


@pytest.mark.asyncio
async def test_this_runs_success_receipt_is_still_reported_as_success(
    monkeypatch, tmp_path,
):
    """Guard against over-correction: a receipt newer than the marker still wins."""
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    (tmp_path / ".update_pending.claimed.json").write_text("{}", encoding="utf-8")
    _write_receipt(tmp_path, outcome="success",
                   started_at=datetime.now(timezone.utc).isoformat(),
                   post_update={"short_sha": "8b64a2af"})
    runner, adapter = make_restart_runner()

    await _conclude(runner, adapter, tmp_path)

    assert "finished" in adapter.sent[0]
    assert "8b64a2af" in adapter.sent[0]
