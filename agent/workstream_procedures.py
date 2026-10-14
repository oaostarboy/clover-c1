"""Ordinary foreground execution evidence, not a model's completion claim.

The host calls begin_execution_turn before its loop and finish_execution_turn
only after the real loop/finalizer has returned. SessionDB readback fences the
result: imported history, missing results, background launch acknowledgements,
blocked calls and nonzero exits never prove a successful workflow. We attest
only the exact foreground commands' exits, NOT arbitrary task completion,
validation coverage, file saves or human delivery. Drafts never grant authority.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from agent import workstreams as ws

logger = logging.getLogger(__name__)
_MAX_ROWS = 80


@dataclass(frozen=True)
class ExecutionTurn:
    scope: ws.Scope
    session_id: str
    floor: int
    imported_call_ids: frozenset[str]


def _enabled(settings: Any) -> bool:
    return bool(settings.enabled and settings.active and settings.procedures_enabled)


def _scope_digest(scope: ws.Scope) -> str:
    return hashlib.sha256(json.dumps([scope.profile, scope.project, scope.audience, scope.root],
                                     ensure_ascii=False).encode()).hexdigest()[:24]


def begin_execution_turn(agent: Any, history: Any = None) -> ExecutionTurn | None:
    """Read-only runtime fence; disabled/shadow do not even open the ledger."""
    try:
        settings = ws.load_settings()
        if not _enabled(settings):
            return None
        scope = ws.scope_for_agent(agent)
        if not ws._usable(scope, settings) or scope.profile != ws.current_profile():
            return None
        # Cron transport identity is not an authentic source conversation.
        if str(getattr(agent, "platform", "")) == "cron":
            return None
        db = getattr(agent, "_session_db", None)
        sid = str(getattr(agent, "session_id", "") or "")
        if db is None or not sid:
            return None
        tail = db.get_messages(sid, latest=True, limit=1)
        imported = frozenset(str(c.get("id")) for m in (history or []) if isinstance(m, dict)
                             for c in (m.get("tool_calls") or []) if isinstance(c, dict))
        return ExecutionTurn(scope, sid, int(tail[-1]["id"]) if tail else 0, imported)
    except Exception:
        logger.debug("execution turn fence unavailable", exc_info=True)
        return None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _workflow(rows: list[dict], imported: frozenset[str] = frozenset()) -> tuple[str, str]:
    """Typed owner result -> status and transferable command sequence.

    Deliberately narrow: 2..8 distinct, simple foreground terminal commands;
    no pipelines, shell masking/control operators, redacted secrets, hints,
    detached/PTY jobs, other tools, missing calls or missing results. An exit
    of zero means those commands ran successfully, nothing broader.
    """
    calls: dict[str, dict] = {}
    results: dict[str, dict] = {}
    for row in rows:
        if row.get("role") == "assistant":
            for call in row.get("tool_calls") or []:
                cid = str(call.get("id") or "")
                if not cid or cid in imported or cid in calls:
                    return "unknown", ""
                calls[cid] = call
        elif row.get("role") == "tool":
            cid = str(row.get("tool_call_id") or "")
            if not cid or cid in results or row.get("effect_disposition") in ("none", "unknown"):
                return "unknown", ""
            results[cid] = row
    if not calls or set(calls) != set(results):
        return "unknown", ""
    commands = []
    for cid, call in calls.items():
        fn = call.get("function") or {}
        args = _json(fn.get("arguments"))
        result = _json(results[cid].get("content"))
        if fn.get("name") != "terminal" or not isinstance(args, dict) or not isinstance(result, dict):
            return "unknown", ""
        if result.get("error") or (type(result.get("exit_code")) is int and result["exit_code"] != 0):
            return "failed", ""
        if type(result.get("exit_code")) is not int or result["exit_code"] != 0:
            return "unknown", ""
        if any(args.get(k) or result.get(k) for k in ("background", "pty", "session_id", "failure_hint")):
            return "unknown", ""
        command = str(args.get("command") or "").strip()
        if not command or re.search(r"[;&|><`$\n\r]", command) or ws._redact(command) != command:
            return "unknown", ""
        try:
            if not shlex.split(command):
                return "unknown", ""
        except ValueError:
            return "unknown", ""
        commands.append(command)
    steps = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(commands))
    if not 2 <= len(commands) <= 8 or len(set(commands)) != len(commands) or len(steps.encode()) > 512:
        return "unknown", ""
    return "success", steps


def _key(steps: str) -> str:
    return "foreground-workflow-" + hashlib.sha256(steps.encode()).hexdigest()[:24]


def _source(scope: ws.Scope, sid: str, first: int, last: int) -> str:
    return f"exec:{quote(sid, safe='')}:{first}:{last}:{_scope_digest(scope)}"


def verified_execution_evidence(scope: ws.Scope, evidence: dict, *, db: Any = None) -> bool:
    """Curator re-reads the original owner rows, root and workflow; no prose proof.

    This API is read-only and never treats a HumanOrigin, a consumed delegation
    summary, or a cron transport status as evidence of successful execution.
    """
    owned = db is None
    try:
        if evidence.get("source_kind") != "execution" or evidence.get("trust") != "owner_session":
            return False
        prefix, sid, first, last, digest = evidence["source_id"].split(":")
        if prefix != "exec" or digest != _scope_digest(scope):
            return False
        sid, first, last = unquote(sid), int(first), int(last)
        if first <= 0 or last <= first or last - first > _MAX_ROWS:
            return False
        if owned:
            from pathlib import Path
            from clover_state import SessionDB
            db = SessionDB(Path(scope.profile) / "state.db", read_only=True)
        if db.get_session(sid) is None or db.get_conversation_root(sid) != evidence.get("session_id"):
            return False
        rows = [r for r in db.get_messages(sid, after_id=first - 1, limit=_MAX_ROWS + 1)
                if r["id"] <= last]
        if not rows or rows[0]["id"] != first or rows[0]["role"] != "user" or rows[-1]["id"] != last:
            return False
        if any(r["role"] == "user" for r in rows[1:]):
            return False
        status, steps = _workflow(rows)
        return status == "success" and steps == evidence.get("summary") and _key(steps) == evidence.get("procedure_key")
    except Exception:
        logger.debug("execution evidence failed owner readback", exc_info=True)
        return False
    finally:
        if owned and db is not None:
            db.close()


def finish_execution_turn(agent: Any, result: Any, turn: ExecutionTurn | None) -> dict | None:
    """Post-persistence producer and real curator consumer, called by the host.

    Replay uses the same owner message IDs and notepad's unique source identity.
    No effect occurs below transfer threshold or when outcome/persistence is
    unknown. Returned status is execution-only; no saved/delivered claim.
    """
    if turn is None:
        return None
    try:
        settings = ws.load_settings()
        if not _enabled(settings) or ws.scope_for_agent(agent) != turn.scope:
            return None
        outcome = {"status": "unknown", "execution_ids": [], "drafts": []}
        if not isinstance(result, dict):
            return outcome
        if result.get("failed") or result.get("interrupted"):
            outcome["status"] = "failed" if result.get("failed") else "unknown"
            return outcome
        if not result.get("completed") or result.get("cleanup_errors"):
            return outcome
        if str(getattr(agent, "session_id", "")) != turn.session_id:
            return outcome  # rotation: do not invent a new execution/session
        db = agent._session_db
        rows = db.get_messages(turn.session_id, after_id=turn.floor, limit=_MAX_ROWS + 1)
        if not rows or len(rows) > _MAX_ROWS or rows[0]["role"] != "user":
            return outcome
        if any(r["role"] == "user" for r in rows[1:]):
            return outcome
        status, steps = _workflow(rows, turn.imported_call_ids)
        outcome["status"] = status
        if status != "success":
            return outcome
        # Persisted results must exactly match this real run's tool results,
        # never imported history or a merely model-generated success message.
        live = {str(m.get("tool_call_id")): m.get("content") for m in result.get("messages", [])
                if m.get("role") == "tool"}
        if any(live.get(str(r.get("tool_call_id"))) != r.get("content")
               for r in rows if r["role"] == "tool"):
            outcome["status"] = "unknown"
            return outcome
        root = db.get_conversation_root(turn.session_id) if db.get_session(turn.session_id) else None
        if not root:
            outcome["status"] = "unknown"
            return outcome
        last = max(r["id"] for r in rows if r["role"] == "tool")
        source = _source(turn.scope, turn.session_id, rows[0]["id"], last)
        evidence = {"source_kind": "execution", "source_id": source, "trust": "owner_session",
                    "session_id": root, "summary": steps, "procedure_key": _key(steps)}
        if not verified_execution_evidence(turn.scope, evidence, db=db):
            outcome["status"] = "unknown"
            return outcome
        from cron import notepad as np
        row = np.create_workstream(turn.scope, kind="progress", title="Verified foreground workflows",
            summary="Exact foreground command exits; not task, save or delivery confirmation",
            fingerprint="foreground-workflows", source="runtime", session_id=root,
            max_items=settings.threads_max_items)
        occurrence = np.ensure_occurrence(
            turn.scope, row["workstream_id"], occurrence_key=source,
            source="foreground_execution",
        )
        # Deterministic owner permits an exact post-crash replay to finish the
        # internal write; it never permits another execution or external retry.
        owner = f"execution:{source}"
        claim = np.claim_occurrence(
            turn.scope, row["workstream_id"], occurrence_key=source, claimer=owner,
        )
        if claim is None and not (
            occurrence.get("state") == "claimed" and occurrence.get("claimer") == owner
        ):
            outcome["execution_ids"] = [source]
            from agent.curator import run_procedure_intake
            outcome["drafts"] = run_procedure_intake(settings=settings)
            return outcome
        np.add_receipt(turn.scope, row["workstream_id"], kind="executed", owner="execution",
                       source_id=source, detail="foreground commands exited zero; read back from SessionDB")
        np.add_evidence(turn.scope, row["workstream_id"], status="verified", key="procedure_run", **evidence)
        np.settle_occurrence(
            turn.scope, row["workstream_id"], occurrence_key=source, claimer=owner,
            state="executed", detail="owner evidence committed and read back",
        )
        committed = next(
            (r for r in np.list_scope_evidence(turn.scope)
             if r.get("source_kind") == "execution" and r.get("source_id") == source),
            None,
        )
        if committed is None or not verified_execution_evidence(turn.scope, committed, db=db):
            outcome["status"] = "unknown"
            return outcome
        outcome["execution_ids"] = [source]
        from agent.curator import run_procedure_intake
        outcome["drafts"] = run_procedure_intake(settings=settings)
        return outcome
    except Exception:
        logger.debug("foreground procedure producer unavailable", exc_info=True)
        return {"status": "unknown", "execution_ids": [], "drafts": []}
