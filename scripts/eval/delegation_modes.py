#!/usr/bin/env python3
"""Live delegation-mode eval: does the root agent pick the right mode?

For each case in ``tests/eval/delegation_modes.jsonl`` this runs a fresh chat
session against a real profile, sends the prompt, waits for the root turn and
any helper completion turns to settle, then classifies what the root did from
that profile's SessionDB transcript:

    direct    no ``delegate_task`` spawn
    single    one spawn call with one task
    parallel  one spawn call with >= 2 tasks (or several single-task calls
              made in the same root turn, which is the same fan-out)
    staged    a later dispatch made from a helper-completion turn after the
              earlier stage returned successfully and the root did not just
              repeat that stage's task (a repair re-dispatch is not staged)

Surface driven: the ``tui_gateway`` JSON-RPC backend (``python -m
tui_gateway.entry``), the same process ``clover --tui`` and the desktop app
talk to.  It runs the real ``AIAgent`` root path with platform ``tui``, which
is in scope for the foreground time limit and the up-front hand-off guidance,
and it can receive a detached helper's completion as a new turn.  ``clover
chat -q`` / ``--oneshot`` are NOT used: a non-TTY one-shot is exempt from the
time limit and cannot receive a background completion, so it would measure a
different agent.  Nothing here touches the gateway or messaging platforms.

The scorer (``score_transcript``) is a pure function over SessionDB message
rows; ``tests/eval/test_delegation_modes_eval.py`` exercises it on transcripts
written through the real SessionDB.

Exit status: 0 pass bar met, 1 pass bar missed, 2 harness error (a run could
not be driven, or never settled).  The eval profile must not stop on approval
prompts (``approvals.mode: off`` or equivalent) or a turn will hang until the
turn timeout; pending approvals are counted in the run record.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = REPO_ROOT / "tests" / "eval" / "delegation_modes.jsonl"

MODES = ("direct", "single", "parallel", "staged")

# User-visible text that must never carry internal limits or failed attempts.
LIMIT_RE = re.compile(r"limit|timed? out|timeout|attempt", re.IGNORECASE)

# Plan "Pass" line, as defaults.  Every knob is overridable with --bar K=V.
DEFAULT_BAR: Dict[str, Any] = {
    "min_correct": 13,              # cases whose every rep picked the expected mode ...
    "of": 15,                       # ... out of this many (scaled when --only runs a subset)
    "overrides_correct": True,      # both explicit overrides respected
    "live_evidence_every_rep": True,  # the three live-evidence cases right on every rep
    "max_limit_mentions": 0,
    "max_premature_success": 0,
    "dispatch_within_calls": True,  # cases carrying dispatch_within_calls (horror: 2)
    "max_run_errors": 0,            # harness errors and runs that never settled
}

# Delegation-completion user rows.  The gateway stores them typed
# ``internal_notification``; the TUI/desktop gateway stores
# ``async_delegation_complete``.  The body marker is what identifies WHICH
# delegation came back, so both are required to be read.
_INTERNAL_KINDS = frozenset({"internal_notification", "async_delegation_complete"})
_COMPLETION_MARKER_RE = re.compile(
    r"\[ASYNC DELEGATION (BATCH )?COMPLETE\s+[—–-]+\s+([^\]\s]+)\]"
)
_OK_STATUSES = frozenset({"completed", "success"})

# A sentence that claims the work is finished.  Paired with a guard so the
# honest "a helper is on it, I'll post it when it's ready" never matches.
_CLAIM_RES = (
    re.compile(
        r"(?:\b(?:is|are|was|were)|'s|'re)\s+(?:now\s+|all\s+)?"
        r"(?:done|complete|completed|finished|ready|built|written|compiled)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)(?:'ve|\s+have)\s+(?:just\s+)?(?:finished|completed|built|created|"
        r"written|generated|produced|compiled|fixed|added|done|finalized|wrapped up|put together)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\ball\s+(?:done|set|finished)\b", re.IGNORECASE),
    re.compile(r"^\W*(?:done|finished|complete(?:d)?)\s*[.!:—-]", re.IGNORECASE),
    re.compile(
        r"\bhere(?:'s|\s+is|\s+are)\s+(?:your|the)\s+(?:final|finished|complete|completed|full)\b",
        re.IGNORECASE,
    ),
)
_CLAIM_GUARD_RE = re.compile(
    r"\b(?:will|won'?t|going to|gonna|once|when|whenever|until|as soon as|after|before|"
    r"working on|in progress|still|not yet|not|isn'?t|aren'?t|hasn'?t|haven'?t|can'?t|cannot|"
    r"if|should be|waiting|meanwhile|started|kicked|handed|handing|dispatch\w*|running|"
    r"helpers?|workers?|subagents?)\b",
    re.IGNORECASE,
)

# Goals count as the same task only when near-identical: sibling tasks that
# differ by one word ("Summarise Slack pricing" / "Summarise Teams pricing")
# must stay distinct.  A re-dispatch after a FAILED stage needs no similarity
# at all, it is a repair by definition.
_SAME_RATIO = 0.92
_SAME_JACCARD = 0.85


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    seen = set()
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(case, dict):
            raise ValueError(f"{path}:{lineno}: case must be an object")
        for key in ("id", "prompt", "expected_mode"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"{path}:{lineno}: missing or empty {key!r}")
        if case["expected_mode"] not in MODES:
            raise ValueError(
                f"{path}:{lineno}: expected_mode {case['expected_mode']!r} not in {MODES}"
            )
        if case["id"] in seen:
            raise ValueError(f"{path}:{lineno}: duplicate case id {case['id']!r}")
        seen.add(case["id"])
        within = case.get("dispatch_within_calls")
        if within is not None and (not isinstance(within, int) or isinstance(within, bool) or within < 1):
            raise ValueError(f"{path}:{lineno}: dispatch_within_calls must be a positive int")
        case["live_evidence"] = bool(case.get("live_evidence", False))
        case["override"] = bool(case.get("override", False))
        cases.append(case)
    if not cases:
        raise ValueError(f"{path}: no cases")
    return cases


# ---------------------------------------------------------------------------
# Transcript normalisation
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return str(content)


def _json_obj(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _calls_of(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = msg.get("tool_calls")
    if isinstance(raw, str):
        raw = _json_obj(raw)
    calls: List[Dict[str, Any]] = []
    for call in raw or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else call
        name = fn.get("name")
        args = _json_obj(fn.get("arguments"))
        calls.append(
            {
                "id": str(call.get("id") or call.get("call_id") or ""),
                "name": name if isinstance(name, str) else "",
                "args": args if isinstance(args, dict) else {},
            }
        )
    return calls


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _same_task(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if difflib.SequenceMatcher(None, na, nb).ratio() >= _SAME_RATIO:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= _SAME_JACCARD


def _completion_ok(text: str, batch: bool) -> bool:
    """Did the returned stage finish cleanly (a verified-complete prior stage)?"""
    if batch:
        if "--- ERROR ---" in text:
            return False
        headers = [ln for ln in text.splitlines() if ln.startswith("--- ") and "TASK" in ln]
        if not headers:
            return False
        for header in headers:
            if "✗" in header or "⚠" in header or "TRUNCATED" in header:
                return False
            m = re.search(r"status=(\w+)", header)
            if m and m.group(1) not in _OK_STATUSES:
                return False
        return True
    m = re.search(r"^Status:\s*(\S+)(.*)$", text, re.MULTILINE)
    if not m or m.group(1) not in _OK_STATUSES:
        return False
    return "TRUNCATED" not in m.group(2)


def _claims_done(text: str) -> Optional[str]:
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = sentence.strip()
        if not s or _CLAIM_GUARD_RE.search(s):
            continue
        if any(rx.search(s) for rx in _CLAIM_RES):
            return s
    return None


def _snippet(text: str, start: int, end: int, pad: int = 50) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - pad): end + pad]).strip()


def _dispatch_result(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Accepted / delegation id from the tool-result row of a spawn call."""
    if row is None:
        return {"accepted": False, "delegation_id": None, "missing": True}
    payload = _json_obj(row["text"])
    if not isinstance(payload, dict):
        # A synchronous (non-background) result is plain text/JSON of its own;
        # the call ran, so it was admitted.
        return {"accepted": True, "delegation_id": None, "missing": False}
    status = str(payload.get("status") or "").lower()
    if status in ("rejected", "error", "failed") or (
        "error" in payload and status != "dispatched"
    ):
        return {"accepted": False, "delegation_id": None, "missing": False}
    did = payload.get("delegation_id")
    return {"accepted": True, "delegation_id": str(did) if did else None, "missing": False}


def _rows(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        rows.append(
            {
                "i": i,
                "id": msg.get("id"),
                "role": role,
                "text": _text_of(msg.get("content")),
                "kind": msg.get("display_kind"),
                "ts": msg.get("timestamp"),
                "calls": _calls_of(msg) if role == "assistant" else [],
                "tool_call_id": str(msg.get("tool_call_id") or ""),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------


def _walk(messages: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rows = _rows(messages)
    results = {r["tool_call_id"]: r for r in rows if r["role"] == "tool" and r["tool_call_id"]}

    dispatches: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []
    limit_hits: List[Dict[str, Any]] = []
    premature: List[Dict[str, Any]] = []
    open_calls: List[str] = []
    visible: List[Dict[str, Any]] = []

    pending: Dict[str, Dict[str, Any]] = {}
    turn_kind = "root"
    last_completion: Optional[Dict[str, Any]] = None
    prior_goals: List[str] = []
    ordinal = 0
    verify_calls = 0
    first_human_ts: Optional[float] = None
    first_attempt_row: Optional[int] = None
    first_attempt_ordinal: Optional[int] = None
    human_turns = 0

    for row in rows:
        role = row["role"]
        if role == "user":
            m = _COMPLETION_MARKER_RE.search(row["text"][:600])
            if m:
                batch, did = bool(m.group(1)), m.group(2)
                turn_kind = "completion"
                ok = _completion_ok(row["text"], batch)
                last_completion = {"id": did, "ok": ok, "batch": batch, "row": row["i"]}
                completions.append(last_completion)
                verify_calls = 0
                key = did if did in pending else next(
                    (k for k in pending if did.startswith(k)), None
                )
                if key is None and pending:
                    key = next(iter(pending))
                if key is not None:
                    pending[key]["remaining"] -= 1
                    if batch or pending[key]["remaining"] <= 0:
                        del pending[key]
            elif row["kind"] in _INTERNAL_KINDS:
                turn_kind = "internal"
            else:
                turn_kind = "root"
                human_turns += 1
                if first_human_ts is None and isinstance(row["ts"], (int, float)):
                    first_human_ts = float(row["ts"])
            continue
        if role == "tool":
            continue

        # ---- assistant row ------------------------------------------------
        for call in row["calls"]:
            ordinal += 1
            if call["id"] and call["id"] not in results:
                open_calls.append(call["name"] or "?")
            if call["name"] != "delegate_task":
                if turn_kind != "root":
                    verify_calls += 1
                continue
            args = call["args"]
            action = str(args.get("action") or "spawn").lower()
            if action != "spawn":
                continue
            tasks = args.get("tasks")
            if isinstance(tasks, str):
                tasks = _json_obj(tasks)
            if isinstance(tasks, list):
                task_list = [t for t in tasks if isinstance(t, dict)]
            elif isinstance(args.get("goal"), str) and args["goal"].strip():
                task_list = [{"goal": args["goal"], "tier": args.get("tier")}]
            else:
                task_list = []
            if not task_list:
                continue  # malformed spawn: the tool errors, nothing was handed off
            top_tier = args.get("tier") if isinstance(args.get("tier"), str) else None
            goals = [str(t.get("goal") or "") for t in task_list]
            tiers = [
                (t.get("tier") if isinstance(t.get("tier"), str) and t.get("tier") else top_tier)
                or "(default)"
                for t in task_list
            ]
            res = _dispatch_result(results.get(call["id"]))
            if first_attempt_row is None:
                first_attempt_row = row["i"]
                first_attempt_ordinal = ordinal

            kind = "rejected"
            stage_verify: Optional[int] = None
            if res["accepted"]:
                if turn_kind == "root":
                    kind = "first_wave"
                else:
                    stage_verify = verify_calls
                    verify_calls = 0
                    if last_completion is None or pending or not last_completion["ok"]:
                        kind = "repair"
                    elif prior_goals and all(
                        any(_same_task(g, p) for p in prior_goals) for g in goals
                    ):
                        kind = "repair"
                    else:
                        kind = "staged"
                prior_goals.extend(goals)
                key = res["delegation_id"] or f"call:{call['id'] or ordinal}"
                pending[key] = {"remaining": len(task_list)}
            dispatches.append(
                {
                    "ordinal": ordinal,
                    "row": row["i"],
                    "turn": turn_kind,
                    "n_tasks": len(task_list),
                    "tiers": tiers,
                    "goals": [g[:160] for g in goals],
                    "accepted": res["accepted"],
                    "kind": kind,
                    "delegation_id": res["delegation_id"],
                    "stage_verify_calls": stage_verify,
                }
            )

        text = row["text"].strip()
        if text:
            visible.append({"row": row["i"], "text": text, "has_calls": bool(row["calls"]),
                            "ts": row["ts"]})
            for m in LIMIT_RE.finditer(text):
                limit_hits.append(
                    {"row": row["i"], "match": m.group(0), "snippet": _snippet(text, m.start(), m.end())}
                )
            if pending:
                claim = _claims_done(text)
                if claim:
                    premature.append(
                        {"row": row["i"], "snippet": claim[:240],
                         "pending": sorted(pending)}
                    )

    last = rows[-1] if rows else None
    return {
        "rows": rows,
        "dispatches": dispatches,
        "completions": completions,
        "limit_hits": limit_hits,
        "premature": premature,
        "open_calls": open_calls,
        "pending": pending,
        "visible": visible,
        "tool_calls_total": ordinal,
        "first_human_ts": first_human_ts,
        "first_attempt_row": first_attempt_row,
        "first_attempt_ordinal": first_attempt_ordinal,
        "last_row": last,
        "human_turns": human_turns,
    }


def score_transcript(messages: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Classify one recorded session.  Pure: rows in, plain dict out."""
    w = _walk(messages)
    dispatches = w["dispatches"]
    accepted = [d for d in dispatches if d["accepted"]]
    first_wave = [d for d in accepted if d["kind"] == "first_wave"]
    staged = [d for d in accepted if d["kind"] == "staged"]
    repair = [d for d in accepted if d["kind"] == "repair"]
    rejected = [d for d in dispatches if not d["accepted"]]

    mode = "direct"
    wave_tasks = sum(d["n_tasks"] for d in first_wave)
    if staged:
        mode = "staged"
    elif first_wave:
        distinct: List[str] = []
        for d in first_wave:
            for g in d["goals"]:
                if not any(_same_task(g, e) for e in distinct):
                    distinct.append(g)
        mode = "parallel" if (any(d["n_tasks"] >= 2 for d in first_wave) or len(distinct) >= 2) else "single"
    elif accepted:
        # Accepted dispatches exist but none from the root turn (the root
        # handed off only from an internal turn): still a hand-off, one wave.
        mode = "parallel" if any(d["n_tasks"] >= 2 for d in accepted) else "single"

    t0 = w["first_human_ts"]
    visible = w["visible"]

    def _since(ts: Any) -> Optional[float]:
        if t0 is None or not isinstance(ts, (int, float)):
            return None
        return round(float(ts) - t0, 3)

    first_text = visible[0] if visible else None
    ack = None
    if w["first_attempt_row"] is not None:
        ack = next((v for v in visible if v["row"] >= w["first_attempt_row"]), None)
    else:
        ack = next((v for v in visible if not v["has_calls"]), first_text)

    tiers = [t for d in accepted for t in d["tiers"]]
    n_pre = (w["first_attempt_ordinal"] - 1) if w["first_attempt_ordinal"] is not None else None
    return {
        "mode": mode,
        "accepted_dispatches": len(accepted),
        "rejected_dispatches": len(rejected),
        "staged_dispatches": len(staged),
        "repair_dispatches": len(repair),
        "first_wave_tasks": wave_tasks,
        "stage_verify_calls": [d["stage_verify_calls"] for d in staged],
        "tool_calls_total": w["tool_calls_total"],
        "first_dispatch_ordinal": w["first_attempt_ordinal"],
        "tool_calls_before_first_dispatch": n_pre,
        "tiers": tiers,
        "completion_turns": len(w["completions"]),
        "ack_seconds": _since(ack["ts"]) if ack else None,
        "first_text_seconds": _since(first_text["ts"]) if first_text else None,
        "limit_hits": w["limit_hits"],
        "premature_success": w["premature"],
        "dispatches": [
            {k: d[k] for k in ("ordinal", "turn", "n_tasks", "tiers", "goals", "accepted",
                               "kind", "delegation_id")}
            for d in dispatches
        ],
        "final_text": (visible[-1]["text"][:400] if visible else ""),
    }


def pending_work(messages: Sequence[Dict[str, Any]]) -> List[str]:
    """Reasons the transcript is not finished (empty list = settled)."""
    w = _walk(messages)
    reasons = [f"tool call {name} has no result" for name in w["open_calls"]]
    reasons += [f"delegation {key} has not returned" for key in w["pending"]]
    last = w["last_row"]
    if last is not None:
        if last["role"] == "user":
            reasons.append("last turn has not been answered")
        elif last["role"] == "tool":
            reasons.append("last turn is mid-flight")
    return reasons


def judge_run(score: Optional[Dict[str, Any]], case: Dict[str, Any]) -> Dict[str, Any]:
    mode = (score or {}).get("mode")
    correct = mode is not None and mode == case["expected_mode"]
    within = case.get("dispatch_within_calls")
    ordinal = (score or {}).get("first_dispatch_ordinal")
    within_ok = True if within is None else (ordinal is not None and ordinal <= within)
    return {
        "correct": correct,
        "override_violated": bool(case.get("override")) and not correct,
        "dispatch_within_calls_ok": within_ok,
    }


# ---------------------------------------------------------------------------
# Pass bar
# ---------------------------------------------------------------------------


def parse_bar_overrides(items: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or key not in DEFAULT_BAR:
            raise ValueError(f"unknown pass-bar knob {item!r}; known: {sorted(DEFAULT_BAR)}")
        default = DEFAULT_BAR[key]
        raw = raw.strip()
        if isinstance(default, bool):
            if raw.lower() not in ("1", "0", "true", "false", "yes", "no"):
                raise ValueError(f"{key} expects true/false, got {raw!r}")
            out[key] = raw.lower() in ("1", "true", "yes")
        else:
            out[key] = int(raw)
    return out


def evaluate_bar(
    runs: Sequence[Dict[str, Any]],
    cases: Sequence[Dict[str, Any]],
    bar: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = {**DEFAULT_BAR, **(bar or {})}
    by_case: Dict[str, List[Dict[str, Any]]] = {c["id"]: [] for c in cases}
    for run in runs:
        by_case.setdefault(run["case_id"], []).append(run)

    def all_correct(case_id: str) -> bool:
        rs = by_case.get(case_id) or []
        return bool(rs) and all(r.get("correct") for r in rs)

    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    n_cases = len(cases)
    need = math.ceil(cfg["min_correct"] * n_cases / cfg["of"]) if cfg["of"] else cfg["min_correct"]
    got = sum(1 for c in cases if all_correct(c["id"]))
    add("cases_correct", got >= need, f"{got}/{n_cases} cases right on every rep (need {need})")

    if cfg["overrides_correct"]:
        ov = [c["id"] for c in cases if c.get("override")]
        bad = [i for i in ov if not all_correct(i)]
        add("overrides_correct", not bad,
            f"{len(ov) - len(bad)}/{len(ov)} overrides respected" + (f"; wrong: {bad}" if bad else ""))
    else:
        add("overrides_correct", True, "not required")

    if cfg["live_evidence_every_rep"]:
        ev = [c["id"] for c in cases if c.get("live_evidence")]
        bad = [i for i in ev if not all_correct(i)]
        add("live_evidence_every_rep", not bad,
            f"{len(ev) - len(bad)}/{len(ev)} live-evidence cases right on every rep"
            + (f"; wrong: {bad}" if bad else ""))
    else:
        add("live_evidence_every_rep", True, "not required")

    hits = sum(len((r.get("score") or {}).get("limit_hits") or []) for r in runs)
    add("limit_mentions", hits <= cfg["max_limit_mentions"],
        f"{hits} internal-limit mention(s) in user-visible text (max {cfg['max_limit_mentions']})")

    prem = sum(len((r.get("score") or {}).get("premature_success") or []) for r in runs)
    add("premature_success", prem <= cfg["max_premature_success"],
        f"{prem} premature success claim(s) (max {cfg['max_premature_success']})")

    if cfg["dispatch_within_calls"]:
        gated = [c for c in cases if c.get("dispatch_within_calls")]
        bad = [c["id"] for c in gated
               if not all(r.get("dispatch_within_calls_ok") for r in by_case.get(c["id"]) or [None])]
        add("dispatch_within_calls", not bad,
            f"{len(gated) - len(bad)}/{len(gated)} gated cases dispatched in time"
            + (f"; late: {bad}" if bad else ""))
    else:
        add("dispatch_within_calls", True, "not required")

    errs = sum(1 for r in runs if r.get("error") or not r.get("settled", True))
    add("run_errors", errs <= cfg["max_run_errors"],
        f"{errs} run(s) errored or never settled (max {cfg['max_run_errors']})")

    return {"passed": all(c["ok"] for c in checks), "checks": checks, "bar": cfg}


# ---------------------------------------------------------------------------
# Settling and reading
# ---------------------------------------------------------------------------


def wait_settled(
    read_messages: Callable[[], Sequence[Dict[str, Any]]],
    is_idle: Callable[[], bool],
    *,
    quiet_seconds: float,
    timeout: float,
    poll_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until the root and every helper it started have gone quiet.

    Settled means: no unanswered turn, no open tool call, every accepted
    background dispatch has returned and been answered, the gateway reports no
    running turn, and nothing new has been written for ``quiet_seconds``.
    """
    start = clock()
    last_sig: Any = object()
    last_change = start
    while True:
        msgs = read_messages()
        sig = (len(msgs), msgs[-1].get("id") if msgs else None)
        now = clock()
        if sig != last_sig:
            last_sig, last_change = sig, now
        if not pending_work(msgs) and is_idle() and now - last_change >= quiet_seconds:
            return True
        if now - start >= timeout:
            return False
        sleep(poll_seconds)


def read_transcript(db_path: Path, session_id: str) -> List[Dict[str, Any]]:
    """Read one session's rows through the real SessionDB (read-only handle)."""
    from clover_state import SessionDB

    db = SessionDB(db_path=Path(db_path), read_only=True)
    try:
        return db.get_messages(session_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Driver: tui_gateway JSON-RPC over stdio
# ---------------------------------------------------------------------------


class RpcEventTracker:
    """Folds JSON-RPC frames from the gateway into turn/response state."""

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id
        self.ready = False
        self.running_turns = 0
        self.completed_turns = 0
        self.approvals = 0
        self.errors: List[str] = []
        self.responses: Dict[Any, Dict[str, Any]] = {}
        self.last_complete: Dict[str, Any] = {}

    def feed(self, frame: Dict[str, Any]) -> None:
        if not isinstance(frame, dict):
            return
        if "id" in frame and ("result" in frame or "error" in frame):
            self.responses[frame["id"]] = {k: frame[k] for k in ("result", "error") if k in frame}
            return
        if frame.get("method") != "event":
            return
        params = frame.get("params") or {}
        kind = params.get("type")
        if kind == "gateway.ready":
            self.ready = True
            return
        if self.session_id is None or params.get("session_id") != self.session_id:
            return
        payload = params.get("payload") or {}
        if kind == "message.start":
            self.running_turns += 1
        elif kind == "message.complete":
            self.running_turns = max(0, self.running_turns - 1)
            self.completed_turns += 1
            self.last_complete = payload if isinstance(payload, dict) else {}
        elif kind == "approval.request":
            self.approvals += 1
        elif kind == "error":
            self.errors.append(str((payload or {}).get("message") or "error"))


def resolve_profile_home(profile: str) -> Path:
    from clover_cli.profiles import get_profile_dir, profile_exists

    if not profile_exists(profile):
        raise SystemExit(f"profile {profile!r} does not exist")
    return Path(get_profile_dir(profile))


class _GatewaySession:
    def __init__(self, conn: "_GatewayConnection", live_id: str, stored_id: str):
        self._conn = conn
        self._live_id = live_id
        self.stored_session_id = stored_id

    def submit(self, prompt: str) -> None:
        resp = self._conn.request(
            "prompt.submit", {"session_id": self._live_id, "text": prompt}, timeout=60
        )
        if "error" in resp:
            raise RuntimeError(f"prompt.submit failed: {resp['error']}")

    def wait_first_turn(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        tr = self._conn.tracker
        while time.monotonic() < deadline:
            self._conn.pump(0.5)
            if tr.errors:
                raise RuntimeError("gateway reported: " + "; ".join(tr.errors[:3]))
            if tr.completed_turns >= 1:
                return True
        return False

    def idle(self) -> bool:
        self._conn.pump(0)
        return self._conn.tracker.running_turns == 0

    @property
    def approvals(self) -> int:
        return self._conn.tracker.approvals

    def interrupt(self) -> None:
        try:
            self._conn.request("session.interrupt", {"session_id": self._live_id}, timeout=10)
        except Exception:
            pass


class _GatewayConnection:
    def __init__(self, home: Path, python: str, repo_root: Path, ready_timeout: float):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLOVER_DESKTOP", "CLOVER_DESKTOP_TERMINAL")}
        env["CLOVER_HOME"] = str(home)
        env["CLOVER_PYTHON_SRC_ROOT"] = str(repo_root)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
        self.proc = subprocess.Popen(
            [python, "-m", "tui_gateway.entry"],
            cwd=str(repo_root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.tracker = RpcEventTracker()
        self._frames: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._stderr: Deque[str] = deque(maxlen=40)
        self._next_id = 0
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        deadline = time.monotonic() + ready_timeout
        while not self.tracker.ready:
            if time.monotonic() > deadline:
                raise RuntimeError("gateway did not become ready: " + self._tail())
            self.pump(0.5)

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                self._frames.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self._frames.put(None)

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            self._stderr.append(line.rstrip())

    def _tail(self) -> str:
        return " | ".join(list(self._stderr)[-8:]) or "(no stderr)"

    def pump(self, wait: float) -> None:
        try:
            frame = self._frames.get(timeout=wait) if wait > 0 else self._frames.get_nowait()
        except queue.Empty:
            return
        while True:
            if frame is None:
                raise RuntimeError(f"gateway exited (code {self.proc.poll()}): {self._tail()}")
            self.tracker.feed(frame)
            try:
                frame = self._frames.get_nowait()
            except queue.Empty:
                return

    def request(self, method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                          "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while rid not in self.tracker.responses:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{method} timed out after {timeout:.0f}s")
            self.pump(0.5)
        return self.tracker.responses.pop(rid)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


class TuiGatewayDriver:
    """One gateway process per run, so completion queues never cross cases."""

    def __init__(self, home: Path, *, python: Optional[str] = None,
                 repo_root: Path = REPO_ROOT, ready_timeout: float = 90.0):
        self.home = Path(home)
        self.python = python or sys.executable
        self.repo_root = repo_root
        self.ready_timeout = ready_timeout

    @contextmanager
    def session(self, *, model: str, provider: Optional[str], cwd: str) -> Iterator[_GatewaySession]:
        conn = _GatewayConnection(self.home, self.python, self.repo_root, self.ready_timeout)
        try:
            params: Dict[str, Any] = {"model": model, "cwd": cwd, "cols": 120}
            if provider:
                params["provider"] = provider
            resp = conn.request("session.create", params, timeout=60)
            if "error" in resp:
                raise RuntimeError(f"session.create failed: {resp['error']}")
            result = resp.get("result") or {}
            live, stored = result.get("session_id"), result.get("stored_session_id")
            if not live or not stored:
                raise RuntimeError(f"session.create returned no ids: {result!r}")
            conn.tracker.session_id = live
            yield _GatewaySession(conn, live, stored)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_eval(
    cases: Sequence[Dict[str, Any]],
    driver: Any,
    *,
    reader: Callable[[str], Sequence[Dict[str, Any]]],
    root_model: str,
    root_provider: Optional[str],
    reps: int,
    evidence_reps: int,
    out_dir: Path,
    turn_timeout: float,
    settle_timeout: float,
    quiet_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    out_dir = Path(out_dir)
    (out_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    runs: List[Dict[str, Any]] = []
    say = progress or (lambda _m: None)
    for case in cases:
        n = max(reps, evidence_reps) if case.get("live_evidence") else reps
        for rep in range(n):
            run: Dict[str, Any] = {
                "case_id": case["id"], "rep": rep, "expected_mode": case["expected_mode"],
                "session_id": None, "error": None, "settled": False,
                "turn_seconds": None, "approvals": 0, "score": None,
                "correct": False, "override_violated": bool(case.get("override")),
                "dispatch_within_calls_ok": case.get("dispatch_within_calls") is None,
            }
            messages: Sequence[Dict[str, Any]] = []
            try:
                cwd = out_dir / "work" / f"{case['id']}-r{rep}"
                cwd.mkdir(parents=True, exist_ok=True)
                prompt = case["prompt"].replace("{workdir}", str(cwd))
                with driver.session(model=root_model, provider=root_provider, cwd=str(cwd)) as sess:
                    sid = sess.stored_session_id
                    run["session_id"] = sid
                    t0 = clock()
                    sess.submit(prompt)
                    ended = sess.wait_first_turn(turn_timeout)
                    run["turn_seconds"] = round(clock() - t0, 2)
                    if not ended:
                        run["error"] = f"root turn did not end within {turn_timeout:.0f}s"
                    run["settled"] = wait_settled(
                        lambda: reader(sid), sess.idle,
                        quiet_seconds=quiet_seconds, timeout=settle_timeout,
                        poll_seconds=poll_seconds, clock=clock, sleep=sleep,
                    )
                    if not run["settled"]:
                        sess.interrupt()
                    run["approvals"] = getattr(sess, "approvals", 0)
                messages = reader(run["session_id"])
                run["score"] = score_transcript(messages)
                run.update(judge_run(run["score"], case))
            except Exception as exc:  # one bad run must not lose the others
                run["error"] = f"{type(exc).__name__}: {exc}"
                if run["session_id"] and not messages:
                    try:
                        messages = reader(run["session_id"])
                        run["score"] = score_transcript(messages)
                        run.update(judge_run(run["score"], case))
                    except Exception:
                        pass
            try:
                (out_dir / "transcripts" / f"{case['id']}-r{rep}.json").write_text(
                    json.dumps(list(messages), indent=1, default=str), encoding="utf-8"
                )
            except OSError:
                pass
            runs.append(run)
            sc = run["score"] or {}
            say(f"{case['id']} r{rep}: expected {case['expected_mode']}, got {sc.get('mode')}"
                + ("" if not run["error"] else f"  [{run['error']}]"))
    return runs


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def render_summary(
    runs: Sequence[Dict[str, Any]],
    cases: Sequence[Dict[str, Any]],
    bar_result: Dict[str, Any],
    meta: Dict[str, Any],
) -> str:
    lines = [
        "Delegation-mode eval",
        f"  profile={meta.get('profile')}  root={meta.get('root_model')}  "
        f"cases={len(cases)}  runs={len(runs)}",
        "",
        f"{'case':<26}{'expect':<10}{'got (per rep)':<30}{'ok':<5}{'calls':<7}"
        f"{'tiers':<18}{'ack_s':<8}{'limit':<7}{'prem':<5}",
    ]
    flag = {c["id"]: ("*" if c.get("live_evidence") else "") + ("O" if c.get("override") else "")
            for c in cases}
    for case in cases:
        rs = [r for r in runs if r["case_id"] == case["id"]]
        got = ",".join((r.get("score") or {}).get("mode") or "ERR" for r in rs)
        ok = "Y" if rs and all(r.get("correct") for r in rs) else "N"
        scores = [r.get("score") or {} for r in rs]
        calls = ",".join(str(s.get("first_dispatch_ordinal") if s.get("first_dispatch_ordinal") is not None else "-") for s in scores)
        tiers = sorted({t for s in scores for t in s.get("tiers", [])})
        acks = [s.get("ack_seconds") for s in scores if s.get("ack_seconds") is not None]
        limit = sum(len(s.get("limit_hits") or []) for s in scores)
        prem = sum(len(s.get("premature_success") or []) for s in scores)
        name = (case["id"] + (" " + flag[case["id"]] if flag[case["id"]] else ""))[:25]
        lines.append(
            f"{name:<26}{case['expected_mode']:<10}{got[:29]:<30}{ok:<5}{calls[:6]:<7}"
            f"{(','.join(tiers) or '-')[:17]:<18}"
            f"{(f'{sum(acks) / len(acks):.1f}' if acks else '-'):<8}{limit:<7}{prem:<5}"
        )
    lines += ["", "  * live-evidence case   O explicit-override case",
              "  calls = position of the first delegate_task attempt among all tool calls", ""]
    problems = [r for r in runs if r.get("error") or not r.get("settled", True)]
    for r in problems:
        why = r.get("error") or "never settled (raise --settle-timeout and rerun this case)"
        lines.append(f"  ! {r['case_id']} r{r['rep']}: {why}")
    hits = [(r, h) for r in runs for h in (r.get("score") or {}).get("limit_hits", [])]
    for r, h in hits[:10]:
        lines.append(f"  ! limit wording in {r['case_id']} r{r['rep']}: ...{h['snippet']}...")
    prem = [(r, p) for r in runs for p in (r.get("score") or {}).get("premature_success", [])]
    for r, p in prem[:10]:
        lines.append(f"  ! premature success in {r['case_id']} r{r['rep']}: {p['snippet']}")
    if problems or hits or prem:
        lines.append("")
    lines.append("Pass bar:")
    for c in bar_result["checks"]:
        lines.append(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    lines += ["", f"RESULT: {'PASS' if bar_result['passed'] else 'FAIL'}"]
    return "\n".join(lines) + "\n"


def write_report(
    out_dir: Path,
    runs: Sequence[Dict[str, Any]],
    cases: Sequence[Dict[str, Any]],
    bar_result: Dict[str, Any],
    meta: Dict[str, Any],
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps({"meta": meta, "bar": bar_result, "cases": list(cases), "runs": list(runs)},
                   indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "summary.txt").write_text(
        render_summary(runs, cases, bar_result, meta), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _delegation_config(home: Path) -> Dict[str, Any]:
    try:
        import yaml

        data = yaml.safe_load((Path(home) / "config.yaml").read_text(encoding="utf-8")) or {}
        deleg = data.get("delegation")
        return deleg if isinstance(deleg, dict) else {}
    except Exception:
        return {}


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live delegation-mode eval (direct / single / parallel / staged).")
    p.add_argument("--profile", required=True, help="Clover profile to drive (its real config, tiers and SessionDB)")
    p.add_argument("--root-model", required=True, help="Root model for the session (per-session override)")
    p.add_argument("--root-provider", default=None, help="Provider for --root-model, when not inferable")
    p.add_argument("--reps", type=int, default=1, help="Repetitions per case (default 1)")
    p.add_argument("--evidence-reps", type=int, default=3,
                   help="Minimum repetitions for live-evidence cases (default 3)")
    p.add_argument("--out", required=True, help="Output directory (results.json, summary.txt, transcripts/)")
    p.add_argument("--cases", default=str(DEFAULT_CASES), help="Cases JSONL")
    p.add_argument("--only", action="append", default=[], help="Run only these case ids (comma separated, repeatable)")
    p.add_argument("--turn-timeout", type=float, default=420.0, help="Seconds to wait for the root turn to end")
    p.add_argument("--settle-timeout", type=float, default=900.0, help="Seconds to wait for helpers and completion turns")
    p.add_argument("--quiet-seconds", type=float, default=20.0, help="Required idle time before a run counts as settled")
    p.add_argument("--poll-seconds", type=float, default=3.0)
    p.add_argument("--bar", action="append", default=[], metavar="KEY=VALUE",
                   help=f"Override a pass-bar knob; knobs: {', '.join(DEFAULT_BAR)}")
    p.add_argument("--dry-run", action="store_true", help="Print the run plan and exit; no model is called")
    return p.parse_args(argv)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    driver_factory: Optional[Callable[[Path, argparse.Namespace], Any]] = None,
    reader_factory: Optional[Callable[[Path], Callable[[str], Sequence[Dict[str, Any]]]]] = None,
) -> int:
    args = _parse_args(argv)
    try:
        cases = load_cases(Path(args.cases))
        bar_over = parse_bar_overrides(args.bar)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = {i.strip() for chunk in args.only for i in chunk.split(",") if i.strip()}
        unknown = wanted - {c["id"] for c in cases}
        if unknown:
            print(f"error: unknown case id(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c["id"] in wanted]

    if args.dry_run:
        for c in cases:
            n = max(args.reps, args.evidence_reps) if c.get("live_evidence") else args.reps
            tags = ("live-evidence " if c.get("live_evidence") else "") + ("override" if c.get("override") else "")
            print(f"{c['id']:<26} expect={c['expected_mode']:<9} reps={n}  {tags}".rstrip())
        return 0

    home = resolve_profile_home(args.profile)
    driver = (driver_factory or (lambda h, a: TuiGatewayDriver(h)))(home, args)
    reader = (reader_factory or (lambda h: (lambda sid: read_transcript(h / "state.db", sid))))(home)
    out_dir = Path(args.out)
    runs = run_eval(
        cases, driver, reader=reader, root_model=args.root_model, root_provider=args.root_provider,
        reps=args.reps, evidence_reps=args.evidence_reps, out_dir=out_dir,
        turn_timeout=args.turn_timeout, settle_timeout=args.settle_timeout,
        quiet_seconds=args.quiet_seconds, poll_seconds=args.poll_seconds,
        progress=lambda m: print(m, file=sys.stderr, flush=True),
    )
    bar_result = evaluate_bar(runs, cases, bar_over)
    meta = {
        "profile": args.profile, "root_model": args.root_model, "root_provider": args.root_provider,
        "surface": "tui_gateway JSON-RPC (platform tui)",
        "reps": args.reps, "evidence_reps": args.evidence_reps,
        "delegation_config": _delegation_config(home),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    write_report(out_dir, runs, cases, bar_result, meta)
    print(render_summary(runs, cases, bar_result, meta))
    if any(r.get("error") or not r.get("settled", True) for r in runs):
        return 2
    return 0 if bar_result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
