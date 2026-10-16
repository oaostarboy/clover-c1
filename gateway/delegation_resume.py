"""Start again the background subagents a gateway restart cut off.

``tools.delegation_inflight`` keeps one small record per running background
dispatch of a gateway chat.  A restart kills the daemon threads that run the
subagents; on the next start, after the platforms connect, this pass reads the
records left behind and, for each one:

* relaunches the job ONCE (same tasks, plus a note that it was interrupted and
  where its earlier work is), reusing the old card message when it still
  exists, else a new unified card;
* or, when it cannot (parent conversation gone, record older than 6 h, already
  resumed once, nothing could start it), tells the parent in plain words with
  an ordinary ``unknown`` helper-status completion -- the agent then tells the
  user which job stopped.

A job that restarts fine produces no message of its own.  Off with
``delegation.resume_after_restart: false``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from tools import async_delegation, delegation_inflight

logger = logging.getLogger(__name__)

RESUME_NOTE = (
    "You were interrupted by a restart. Your earlier progress may be in {where}. "
    "Check what is already done, continue from there, and do not start over."
)


def _owner_alive(record: Dict[str, Any]) -> bool:
    """Is the process that dispatched this job still running (not us)?"""
    try:
        from gateway.status import _pid_exists, get_process_start_time

        pid = int(record.get("owner_pid") or 0)
        if not pid or not _pid_exists(pid):
            return False
        started = record.get("owner_started_at")
        if started is not None:
            return get_process_start_time(pid) == int(started)
        return True
    except Exception:
        return False


def _where(record: Dict[str, Any], task: Dict[str, Any], index: int) -> str:
    places: List[str] = []
    if task.get("worktree"):
        places.append(str(task["worktree"]))
    paths = record.get("live_paths") or []
    if isinstance(paths, list) and 0 <= index < len(paths) and paths[index]:
        places.append(f"the live transcript {paths[index]}")
    return " / ".join(places) or "your working folder and your session history"


def build_resume_tasks(record: Dict[str, Any], indexes: List[int]) -> List[Dict[str, Any]]:
    """The tasks to relaunch, each with the interruption note in its context."""
    tasks: List[Dict[str, Any]] = []
    stored = record.get("tasks") or []
    single_handoff = record.get("handoff") if isinstance(record.get("handoff"), dict) else {}
    for i in indexes:
        t = stored[i]
        note = RESUME_NOTE.format(where=_where(record, t, i))
        context = f"{note}\n\n{t['context']}" if t.get("context") else note
        task: Dict[str, Any] = {"goal": t.get("goal") or "", "context": context}
        title = t.get("title") or (record.get("card_work") if len(stored) == 1 else "")
        if title:
            task["title"] = title
        steps = t.get("steps") or (single_handoff.get("steps") if len(stored) == 1 else None)
        if steps:
            task["steps"] = steps
        for key in ("tier", "output_schema", "route", "account", "_route_binding"):
            if t.get(key):
                task[key] = t[key]
        tasks.append(task)
    return tasks


def _decide(record: Dict[str, Any], now: float) -> str:
    """``""`` to resume, else the plain reason it cannot be."""
    if now - float(record.get("dispatched_at") or 0) > delegation_inflight.MAX_RESUME_AGE_SECONDS:
        hours = int(delegation_inflight.MAX_RESUME_AGE_SECONDS // 3600)
        return f"it was started more than {hours} hours ago."
    if int(record.get("resumes") or 0) >= delegation_inflight.MAX_RESUMES:
        return "it had already been started again once and was cut off a second time."
    if not delegation_inflight.parent_session_alive(record):
        return "the conversation it belonged to no longer exists."
    return ""


def _source_from_origin(origin: Dict[str, Any], record: Dict[str, Any]) -> Any:
    """A ``SessionSource`` straight from the route stored at dispatch -- for a
    chat whose session key is a raw id (the API server binds the caller's
    ``X-Clover-Session-Id``), which cannot be derived from a key's layout."""
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource

        platform = Platform(str(origin.get("origin_platform") or "").strip().lower())
        chat_id = str(origin.get("origin_chat_id") or "").strip()
        if not chat_id:
            return None
        return SessionSource(
            platform=platform,
            chat_id=chat_id,
            chat_type=str(origin.get("origin_chat_type") or "dm") or "dm",
            thread_id=str(origin.get("origin_thread_id") or "") or None,
            user_id=str(origin.get("user_id") or "") or None,
            user_name=str(origin.get("user_name") or "") or None,
            scope_id=str(origin.get("scope_id") or "") or None,
        )
    except Exception:
        return None


async def _build_parent(runner: Any, record: Dict[str, Any]):
    """``(agent, source, publisher)`` standing in for the conversation's agent."""
    from gateway.run import _load_gateway_config, _platform_config_key
    from run_agent import AIAgent

    origin = record.get("origin") or {}
    evt = {
        "session_key": record.get("session_key"),
        "platform": origin.get("origin_platform"),
        "chat_type": origin.get("origin_chat_type"),
        "chat_id": origin.get("origin_chat_id"),
        "thread_id": origin.get("origin_thread_id"),
        "user_id": origin.get("user_id"),
        "user_name": origin.get("user_name"),
        "scope_id": origin.get("scope_id"),
    }
    source = await asyncio.to_thread(runner._build_process_event_source, evt)
    if source is None:
        source = _source_from_origin(origin, record)
    if source is None:
        raise RuntimeError("the chat it belonged to cannot be reached")
    user_config = _load_gateway_config()
    model, runtime_kwargs = runner._resolve_session_agent_runtime(
        source=source, session_key=record.get("session_key"), user_config=user_config,
    )
    if not runtime_kwargs.get("api_key"):
        raise RuntimeError("no model credentials are configured")
    platform_key = _platform_config_key(source.platform)
    enabled_toolsets = runner._resolve_enabled_toolsets_for_source(user_config, source, platform_key)
    publisher = None
    try:
        from gateway.delegation_activity import build_turn_publisher

        publisher = build_turn_publisher(runner, source, user_config, platform_key, lambda: True)
    except Exception:
        logger.debug("no card publisher for the resumed job", exc_info=True)

    def _build():
        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=4,
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
            enabled_toolsets=enabled_toolsets,
            session_id=record.get("parent_session_id"),
            platform=platform_key,
            user_id=source.user_id,
            user_id_alt=source.user_id_alt,
            user_name=source.user_name,
            chat_id=source.chat_id,
            chat_name=source.chat_name,
            chat_type=source.chat_type,
            thread_id=source.thread_id,
            gateway_session_key=record.get("session_key"),
            session_db=getattr(runner._session_db, "_db", runner._session_db),
        )
        agent._end_session_on_close = False  # the conversation's session lives on
        return agent

    agent = await runner._run_in_executor_with_context(_build)

    def _relay(event_type, tool_name=None, preview=None, args=None, **kwargs):
        if publisher is not None and isinstance(event_type, str) and event_type.startswith("subagent."):
            try:
                publisher.observe(event_type, tool_name, preview, args, **kwargs)
            except Exception:
                logger.debug("resumed job event relay failed", exc_info=True)

    agent.tool_progress_callback = _relay
    return agent, source, publisher


def _launch(runner: Any, record: Dict[str, Any], agent: Any, source: Any, publisher: Any,
            tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Runs on a worker thread: the same session variables a chat turn has, the
    card publisher bound, then one ``delegate_task`` background dispatch."""
    from agent.delegation_activity import bind_activity_sink
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval import reset_current_session_key, set_current_session_key
    from tools.delegate_tool import delegate_task

    origin = record.get("origin") or {}
    tokens = set_session_vars(
        platform=source.platform.value,
        chat_id=str(source.chat_id),
        chat_type=str(source.chat_type or ""),
        chat_name=source.chat_name or "",
        thread_id=str(source.thread_id or ""),
        user_id=str(source.user_id or ""),
        user_name=str(source.user_name or ""),
        scope_id=str(origin.get("scope_id") or ""),
        session_key=str(record.get("session_key") or ""),
        profile=str(origin.get("origin_profile") or ""),
        async_delivery=True,
        cron_session="",
    )
    key_token = set_current_session_key(str(record.get("session_key") or ""))
    try:
        bind_activity_sink(publisher)
        with delegation_inflight.resume_context(
            int(record.get("resumes") or 0) + 1, str(record["delegation_id"]),
            card=record.get("card"), work=str(record.get("card_work") or ""),
        ):
            raw = delegate_task(
                tasks=tasks,
                role=record.get("role"),
                background=True,
                require_background=True,
                parent_agent=agent,
            )
    finally:
        reset_current_session_key(key_token)
        clear_session_vars(tokens)
    try:
        return json.loads(raw)
    except Exception:
        return {"status": "error", "error": str(raw)[:200]}


async def resume_one(runner: Any, record: Dict[str, Any]) -> str:
    """Resume (or report) one record.  Returns ``resumed`` / ``reported`` / ``dropped``."""
    did = str(record["delegation_id"])
    now = time.time()
    state = None
    try:
        row = async_delegation.get_durable_delegation(did)
        state = row.get("state") if row else None
    except Exception:
        row = None
    if row is None or state not in ("resumed", "running", "finalizing"):
        # Finished (or never recorded): nothing to start again.
        delegation_inflight.remove_record(did)
        return "dropped"

    reason = _decide(record, now)
    if not reason:
        stored = record.get("tasks") or []
        done = async_delegation.finished_child_indexes(did) if len(stored) > 1 else set()
        todo = [i for i in range(len(stored)) if i not in done]
        if not todo:
            delegation_inflight.remove_record(did)
            async_delegation.release_parked_children()
            return "dropped"
        try:
            agent, source, publisher = await _build_parent(runner, record)
            tasks = build_resume_tasks(record, todo)
            payload = await runner._run_in_executor_with_context(
                _launch, runner, record, agent, source, publisher, tasks,
            )
            if payload.get("status") != "dispatched":
                reason = (
                    "it could not be started: "
                    + str(payload.get("error") or payload.get("note") or "the helper pool refused it")[:160]
                )
            else:
                logger.info(
                    "Resumed interrupted delegation %s as %s (%d task(s))",
                    did, payload.get("delegation_id"), len(tasks),
                )
                if publisher is not None:
                    await asyncio.sleep(1.0)  # let the card land before the publisher rests
                    try:
                        await publisher.drain()
                    except Exception:
                        pass
                async_delegation.close_resumed(did)
                delegation_inflight.remove_record(did)
                async_delegation.release_parked_children()
                return "resumed"
        except Exception as exc:
            logger.warning("Could not resume delegation %s", did, exc_info=True)
            reason = f"it could not be started ({type(exc).__name__})."
    logger.warning("Not resuming interrupted delegation %s: %s", did, reason)
    delegation_inflight.remove_record(did)
    async_delegation.report_unresumed(did, reason)
    async_delegation.release_parked_children()
    return "reported"


async def resume_interrupted(runner: Any) -> Dict[str, int]:
    """The start-up pass over every record a previous process left behind."""
    counts = {"resumed": 0, "reported": 0, "dropped": 0}
    records = delegation_inflight.list_records()
    if not records:
        return counts
    enabled = delegation_inflight.resume_enabled()
    this_start = float(getattr(runner, "_gateway_started_at", 0.0) or 0.0)
    for record in records:
        did = str(record.get("delegation_id") or "")
        try:
            # Only what a PREVIOUS process left behind: older than this start,
            # and its dispatching process is gone.
            if this_start and float(record.get("dispatched_at") or 0) >= this_start:
                continue
            if _owner_alive(record):
                continue
            if not enabled:
                delegation_inflight.remove_record(did)
                counts["dropped"] += 1
                continue
            counts[await resume_one(runner, record)] += 1
        except Exception:
            logger.warning("Resume pass failed for %s", did, exc_info=True)
    if any(counts.values()):
        logger.info("Interrupted-delegation pass: %s", counts)
    return counts
