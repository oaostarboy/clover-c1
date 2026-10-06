"""Telegram album/photo continuation: delivery ownership and album identity.

A flush that has already claimed its buffered event owns that delivery.  A
later photo for the same batch key must start its own delivery instead of
cancelling the in-flight one (which re-delivered the first part a second
time).  Album identity comes only from Telegram's ``media_group_id``; photos
without one are never given an album identity and are never merged by
arrival timing.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter

ALBUM_KEY = "telegram_media_group_id"


class _Dispatch:
    """Stands in for ``handle_message``; holds the first delivery mid-flight."""

    def __init__(self):
        self.events = []
        self.delivered = []
        self.cancelled = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, event):
        self.events.append(event)
        self.delivered.append(list(event.media_urls))
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


@pytest.fixture()
def adapter():
    a = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    a._is_callback_user_authorized = lambda user_id, **_kw: True
    a.MEDIA_GROUP_WAIT_SECONDS = 0.01
    return a


def _photo_update(message_id, *, media_group_id=None, caption=None):
    file_obj = AsyncMock()
    file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
    file_obj.file_path = "photos/file.jpg"
    photo = MagicMock()
    photo.get_file = AsyncMock(return_value=file_obj)

    msg = MagicMock()
    msg.message_id = message_id
    msg.text = caption or ""
    msg.caption = caption
    msg.date = None
    msg.photo = [photo]
    msg.video = None
    msg.audio = None
    msg.voice = None
    msg.sticker = None
    msg.document = None
    msg.media_group_id = media_group_id
    msg.chat = MagicMock()
    msg.chat.id = 100
    msg.chat.type = "private"
    msg.chat.title = None
    msg.chat.full_name = "Test User"
    msg.from_user = MagicMock()
    msg.from_user.id = 1
    msg.from_user.full_name = "Test User"
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.message = msg
    return update


async def _receive(adapter, update, cached_path):
    with patch(
        "plugins.platforms.telegram.adapter.cache_image_from_bytes",
        return_value=cached_path,
    ):
        await adapter._handle_media_message(update, MagicMock())


async def _settle(adapter, dispatch, expected):
    """Let flushes and any held-event redispatch run to completion."""
    for _ in range(200):
        await asyncio.sleep(0.01)
        pending = (
            list(adapter._media_group_tasks.values())
            + list(adapter._pending_photo_batch_tasks.values())
        )
        redispatch = adapter._held_inbound_redispatch_task
        if redispatch is not None:
            pending.append(redispatch)
        if len(dispatch.delivered) >= expected and all(t.done() for t in pending):
            break
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_late_album_item_does_not_redeliver_the_part_already_in_flight(adapter):
    """Same media_group_id, second item lands while the first is being dispatched."""
    dispatch = _Dispatch()
    adapter.handle_message = dispatch

    await _receive(adapter, _photo_update(1, media_group_id="album-1", caption="first"), "/tmp/a.jpg")
    await asyncio.wait_for(dispatch.entered.wait(), 1)
    await _receive(adapter, _photo_update(2, media_group_id="album-1"), "/tmp/b.jpg")
    await asyncio.sleep(0)
    dispatch.release.set()
    await _settle(adapter, dispatch, expected=2)

    # The in-flight delivery kept its ownership; nothing was re-held.
    assert dispatch.cancelled == 0
    assert adapter._held_inbound_events == []
    # Every distinct input is delivered exactly once.
    assert dispatch.delivered == [["/tmp/a.jpg"], ["/tmp/b.jpg"]]
    # Both parts carry the album identity Telegram gave them.
    assert [e.metadata.get(ALBUM_KEY) for e in dispatch.events] == ["album-1", "album-1"]


@pytest.mark.asyncio
async def test_album_items_inside_the_window_are_one_event_with_identity(adapter, caplog):
    dispatch = _Dispatch()
    dispatch.release.set()
    adapter.handle_message = dispatch

    with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.adapter"):
        await _receive(adapter, _photo_update(1, media_group_id="album-1", caption="PRIVATE-CAPTION"), "/tmp/a.jpg")
        await _receive(adapter, _photo_update(2, media_group_id="album-1"), "/tmp/b.jpg")
        await _settle(adapter, dispatch, expected=1)

    assert dispatch.delivered == [["/tmp/a.jpg", "/tmp/b.jpg"]]
    assert dispatch.events[0].metadata.get(ALBUM_KEY) == "album-1"
    flushes = [r.getMessage() for r in caplog.records if "Flushing media group" in r.getMessage()]
    assert len(flushes) == 1
    assert "album-1" in flushes[0]
    assert "2 item(s)" in flushes[0]
    assert "PRIVATE-CAPTION" not in flushes[0]


@pytest.mark.asyncio
async def test_different_albums_are_never_merged(adapter):
    """Two albums interleaved in time stay two requests with their own captions."""
    dispatch = _Dispatch()
    dispatch.release.set()
    adapter.handle_message = dispatch

    await _receive(adapter, _photo_update(1, media_group_id="album-1", caption="first ask"), "/tmp/a.jpg")
    await _receive(adapter, _photo_update(2, media_group_id="album-2", caption="second ask"), "/tmp/b.jpg")
    await _settle(adapter, dispatch, expected=2)

    by_album = {e.metadata.get(ALBUM_KEY): e for e in dispatch.events}
    assert sorted(by_album) == ["album-1", "album-2"]
    assert by_album["album-1"].media_urls == ["/tmp/a.jpg"]
    assert by_album["album-1"].text == "first ask"
    assert by_album["album-2"].media_urls == ["/tmp/b.jpg"]
    assert by_album["album-2"].text == "second ask"


def _image_document_update(message_id, *, caption=None):
    """An image sent as a file (no media_group_id)."""
    update = _photo_update(message_id, caption=caption)
    msg = update.message
    doc = MagicMock()
    doc.file_name = "shot.png"
    doc.mime_type = "image/png"
    doc.file_size = 1024
    doc.get_file = msg.photo[0].get_file
    msg.photo = None
    msg.document = doc
    return update


@pytest.mark.parametrize("make_update", [_photo_update, _image_document_update])
@pytest.mark.asyncio
async def test_ungrouped_photos_in_the_same_window_stay_separate_requests(adapter, make_update):
    """No media_group_id means no proof of a batch: arrival timing merges nothing."""
    dispatch = _Dispatch()
    dispatch.release.set()
    adapter.handle_message = dispatch
    adapter._media_batch_delay_seconds = 0.5

    await _receive(adapter, make_update(1, caption="first ask"), "/tmp/a.jpg")
    # Delivered as it arrives: there is no timer to wait out.
    assert dispatch.delivered == [["/tmp/a.jpg"]]
    await _receive(adapter, make_update(2, caption="second ask"), "/tmp/b.jpg")
    await _settle(adapter, dispatch, expected=2)

    assert dispatch.delivered == [["/tmp/a.jpg"], ["/tmp/b.jpg"]]
    assert [e.text for e in dispatch.events] == ["first ask", "second ask"]
    assert len({e.message_id for e in dispatch.events}) == 2
    assert all(ALBUM_KEY not in e.metadata for e in dispatch.events)
    assert adapter._pending_photo_batches == {}
