"""Internal, launch-scoped provenance for local terminal-owned CLI workers.

This is transport metadata, not user configuration. Never infer identity from
argv, titles, model names, or the inherited CLOVER_SESSION_ID.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path

_ENV = "_CLOVER_WORKER_LAUNCH"
_launch: ContextVar[str | None] = ContextVar("clover_worker_launch", default=None)


@contextmanager
def local_worker_launch(enabled=False):
    payload = None
    if enabled:
        from gateway.session_context import _SESSION_ID, _UNSET, session_context_engaged
        from clover_constants import get_clover_home

        sid = _SESSION_ID.get()
        if sid is _UNSET:
            sid = "" if session_context_engaged() else os.environ.get("CLOVER_SESSION_ID", "")
        if sid:
            payload = json.dumps({"parent": sid, "home": str(get_clover_home().resolve())})
    token = _launch.set(payload)
    try:
        yield
    finally:
        _launch.reset(token)


def inject_worker_launch(env):
    """Strip inherited provenance; only the current explicit spawn may set it."""
    env.pop(_ENV, None)
    payload = _launch.get()
    if payload:
        env[_ENV] = payload


def configure_oneshot_worker(agent, db):
    """Validate in the child's profile; set both identities before first use."""
    raw = os.environ.get(_ENV)
    if not raw:
        return
    from clover_constants import get_clover_home

    payload = json.loads(raw)
    if (not isinstance(payload, dict)
            or payload.get("home") != str(get_clover_home().resolve())
            or db is None
            or Path(db.db_path).resolve() != (get_clover_home() / "state.db").resolve()):
        raise ValueError("CLI worker provenance does not belong to this profile")
    parent = payload.get("parent")
    if not isinstance(parent, str) or not parent or parent == agent.session_id:
        raise ValueError("Invalid CLI worker parent")
    row = db.get_session(parent)
    if row is None:
        raise ValueError("CLI worker parent is missing from this profile")
    agent._parent_session_id = parent
    agent._session_init_model_config["_delegate_from"] = parent
    agent._validate_worker_parent = True
