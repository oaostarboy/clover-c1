"""Hard-bounded awaits for turn teardown.

``asyncio.wait_for(task, t)`` is *not* a hard bound: on timeout it cancels the
task and then waits for the cancellation to finish.  A task whose
``CancelledError`` handler performs platform I/O (the stream consumer's
best-effort final edit, the progress sender's final flush) or swallows the
cancel therefore pins the awaiting coroutine for as long as that I/O hangs.

For a gateway turn this means a *finished* agent turn keeps the session's
busy guard, so later human messages and background-worker completions queue
behind a phantom turn.  These helpers give up on the cancelled task after a
grace period and let it unwind in the background instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


def _consume_abandoned(task: "asyncio.Future[Any]") -> None:
    """Retrieve an abandoned task's outcome so it is never logged as unretrieved."""
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:  # pragma: no cover - defensive
        pass


_STUCK_TASKS: "set[asyncio.Future[Any]]" = set()


def stuck_task_count() -> int:
    """Tasks released by a bounded teardown that are still unwinding."""
    return len(_STUCK_TASKS)


def _escalate(task: "asyncio.Future[Any]", label: str, grace: float) -> None:
    """A cancelled task is still stuck after ``grace``: release the turn.

    The task gets a SECOND cancellation, which interrupts whatever platform
    await its cancellation handler is parked in (a handler is entered once
    per cancel), so abandoned work is finite rather than parked forever.  It
    is tracked only until it finishes, so a burst of hung turns is visible in
    the log and in :func:`stuck_task_count` but cannot accumulate silently.
    """
    if task in _STUCK_TASKS:
        task.cancel()
        return
    _STUCK_TASKS.add(task)
    task.add_done_callback(_STUCK_TASKS.discard)
    task.add_done_callback(_consume_abandoned)
    logger.warning(
        "Teardown: %s did not finish within %.1fs of cancellation; releasing "
        "the turn, re-cancelling it, and letting it unwind in the background "
        "(%d teardown task(s) unwinding)",
        label,
        grace,
        len(_STUCK_TASKS),
    )
    task.cancel()


def abandon(task: "asyncio.Future[Any]") -> None:
    """Cancel ``task`` without waiting for it; swallow its eventual outcome."""
    if task.done():
        _consume_abandoned(task)
        return
    task.cancel()
    task.add_done_callback(_consume_abandoned)


def _release(task: "asyncio.Future[Any]", label: str) -> None:
    """Give up on ``task`` NOW: cancel, then re-cancel via the tracked path.

    The first cancel lets a well-behaved task unwind; the second (issued on
    the next loop iteration, i.e. after its cancellation handler has started)
    interrupts a handler parked in platform I/O.  Always tracked until done.
    """
    if task.done():
        _consume_abandoned(task)
        return
    task.cancel()
    loop = task.get_loop()
    loop.call_soon(lambda: None if task.done() else _escalate(task, label, 0.0))


async def await_bounded(
    awaitable: Awaitable[Any], timeout: float
) -> Tuple[bool, Optional[Any]]:
    """Await ``awaitable`` for at most ``timeout`` seconds.

    Returns ``(True, result)`` on completion (exceptions from the awaitable
    propagate).  Returns ``(False, None)`` on timeout after cancelling the
    awaitable *without waiting for it to unwind*.  Cancellation of the caller
    cancels the inner awaitable too and propagates.
    """
    fut = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({fut}, timeout=max(0.0, timeout))
    except BaseException:
        _release(fut, "bounded awaitable (caller cancelled)")
        raise
    if fut in done:
        return True, fut.result()
    _release(fut, "bounded awaitable (timed out)")
    return False, None


async def reap_task(
    task: Optional["asyncio.Future[Any]"],
    *,
    grace: float,
    label: str = "task",
    cancel: bool = True,
) -> bool:
    """Cancel ``task`` (optionally) and wait at most ``grace`` seconds for it.

    Returns True when the task finished (its exception, if any, is consumed).
    Returns False when it is still unwinding after ``grace``; it is then left
    to finish in the background and a warning names ``label``.
    """
    if task is None:
        return True
    if cancel and not task.done():
        task.cancel()
    if not task.done():
        try:
            await asyncio.wait({task}, timeout=max(0.0, grace))
        except asyncio.CancelledError:
            # The CALLER was cancelled (Stop/reset/shutdown) while we waited:
            # never leave the helper parked in its first cancellation handler.
            _escalate(task, label, grace)
            raise
    if task.done():
        _consume_abandoned(task)
        return True
    _escalate(task, label, grace)
    return False


async def reap_tasks(
    tasks: Iterable[Optional["asyncio.Future[Any]"]],
    *,
    grace: float,
    labels: Optional[Iterable[str]] = None,
) -> None:
    """Reap several already-cancelled tasks within ONE shared grace window."""
    pending = [t for t in tasks if t is not None]
    names = list(labels) if labels is not None else [f"task-{i}" for i in range(len(pending))]
    if not pending:
        return
    live = [t for t in pending if not t.done()]
    if live:
        try:
            await asyncio.wait(set(live), timeout=max(0.0, grace))
        except asyncio.CancelledError:
            for task, name in zip(pending, names):
                if task.done():
                    _consume_abandoned(task)
                else:
                    _escalate(task, name, grace)
            raise
    for task, name in zip(pending, names):
        if task.done():
            _consume_abandoned(task)
        else:
            _escalate(task, name, grace)


async def drain_then_cancel(
    task: Optional["asyncio.Future[Any]"],
    *,
    drain: float,
    grace: float,
    label: str = "task",
) -> bool:
    """Give ``task`` ``drain`` seconds to finish on its own, then cancel it
    and wait at most ``grace`` more seconds for it to unwind.

    Hard-bounded by ``drain + grace``: unlike ``asyncio.wait_for`` this never
    waits for a cancellation handler that is itself stuck in I/O.  If the
    caller is cancelled while waiting, the task is cancelled (not awaited) and
    the ``CancelledError`` propagates.  Returns True when the task finished.
    """
    if task is None:
        return True
    if not task.done():
        try:
            await asyncio.wait({task}, timeout=max(0.0, drain))
        except asyncio.CancelledError:
            abandon(task)
            raise
    if task.done():
        _consume_abandoned(task)
        return True
    return await reap_task(task, grace=grace, label=label)
