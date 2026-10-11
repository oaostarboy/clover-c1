"""Warm the imports the delegation router's first call needs.

The first router call in a fresh process spends seconds (cold disk: tens of
seconds) importing the auxiliary client, the provider SDKs and the plugin set
before any request is made, so the first routed turn after a start misses its
deadline and gets no plan.  ``start`` does only those imports, in a daemon
thread: no model call, no network, no prompt or tool change.  Plugin loading
goes through the existing background discovery, which every synchronous
consumer joins, so nothing can observe a half-loaded registry.
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Modules the first auxiliary call imports lazily.  Each is optional: a missing
# SDK is simply not warmed.
_MODULES = (
    "agent.auxiliary_client",
    "agent.credential_pool",
    "clover_cli.config",
    "clover_cli.runtime_provider",
    "httpx",
    "requests",
    "openai",
    "anthropic",
)

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_suppressed = False


def suppress() -> None:
    """Never start the warm-up in this process.

    A noninteractive entry point (one-shot, batch worker, ``chat -q``) marks
    its agent exempt only AFTER the constructor returns -- too late for the
    start inside it -- so it calls this before building the agent.
    """
    global _suppressed
    _suppressed = True


def _warm() -> None:
    for name in _MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            logger.debug("router prewarm: %s unavailable", name, exc_info=True)
    try:
        from clover_cli.plugins import discover_plugins

        discover_plugins()  # idempotent; joins an in-flight background scan
    except Exception:
        logger.debug("router prewarm: plugin discovery unavailable", exc_info=True)


def start(agent: Any = None) -> bool:
    """Start the warm-up once per process when the router is enabled.

    ``agent`` (when given) must be one the router would route: a subagent
    (built inside the delegated-child context) or a cron root never starts it,
    and an entry point that called ``suppress`` (one-shot, batch, single-query)
    never does.  Never blocks and never raises; returns whether a thread was
    started.
    """
    global _thread
    try:
        if _suppressed:
            return False
        from agent import foreground_time_limit as fl
        from agent.delegation_router import load_settings

        if agent is not None and not fl.applies(agent):
            return False
        if not load_settings().enabled:
            return False
        with _lock:
            if _thread is not None:
                return False
            _thread = threading.Thread(
                target=_warm, name="delegation-router-prewarm", daemon=True,
            )
            _thread.start()
        return True
    except Exception:
        logger.debug("router prewarm not started", exc_info=True)
        return False
