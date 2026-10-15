"""Bounded, human-declared acceptance at the existing foreground owner seam.

No model proof, subprocess launcher, permission grant or ancestor closure.
A current authenticated human names an immutable local contract before tools.
The owner reads actual current-turn check results, fresh JUnit and frozen
artifacts, then settles only exact named work revisions in one transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from agent import workstreams as ws
from cron import notepad as np


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _read(scope, name, limit=1024 * 1024):
    from agent.work_lifecycle import _audit_bytes
    return _audit_bytes(scope.project, name, limit)


def _artifacts(scope, entries):
    remaining = 4 * 1024 * 1024
    for name, expected in entries.items():
        value = _read(scope, name, remaining)
        remaining -= len(value)
        if hashlib.sha256(value).hexdigest() != expected:
            raise ValueError("artifact revision changed")
    return _digest(entries)


def _evidence(scope, wid):
    return _digest(np.list_evidence(scope, wid))


@dataclass(frozen=True)
class Acceptance:
    scope: ws.Scope
    session_id: str
    event_id: str
    message: str
    path: str
    content: bytes
    evidence: tuple[tuple[str, str], ...]
    started: float


def prepare(agent, message, scope):
    """Freeze explicit coverage/criteria before execution, never from prose."""
    from agent.workstream_origin import ingress_is_valid
    from tools.terminal_tool import _get_env_config
    match = re.fullmatch(r"Verify work acceptance ([A-Za-z0-9][A-Za-z0-9_./-]*)", str(message or ""))
    ingress = getattr(agent, "_trusted_human_ingress", None)
    sid = str(getattr(agent, "session_id", ""))
    if (not match or not scope.private or not ingress_is_valid(ingress, profile=scope.profile)
            or ingress.session_id != sid or _get_env_config().get("env_type") != "local"):
        return None
    try:
        content = _read(scope, match[1], 8192)
        spec = _spec(content)
        _artifacts(scope, spec["artifacts"])
        evidence = []
        for target in spec["coverage"]:
            row = np.get_workstream(scope, target["workstream_id"])
            if (row is None or row["kind"] != "obligation" or row["status"] not in ("open", "deferred")
                    or row["revision"] != target["revision"] or row["session_id"] != sid
                    or target["session_id"] != sid):
                return None
            evidence.append((row["workstream_id"], _evidence(scope, row["workstream_id"])))
        for check in spec["checks"]:
            # A pre-existing report is not fresh execution evidence, even if
            # the later shell command happens to return zero.
            try:
                _read(scope, check["report"])
            except FileNotFoundError:
                pass
            else:
                return None
        return Acceptance(scope, sid, ingress.event_id, message, match[1], content,
                          tuple(evidence), time.monotonic())
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _spec(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate contract key")
            result[key] = value
        return result
    s = json.loads(content, object_pairs_hook=pairs)
    if set(s) != {"version", "coverage", "artifacts", "checks"} or s["version"] != 1:
        raise ValueError("unsupported acceptance contract")
    coverage, checks, artifacts = s["coverage"], s["checks"], s["artifacts"]
    if (not isinstance(coverage, list) or not 1 <= len(coverage) <= 8
            or not isinstance(checks, list) or not 1 <= len(checks) <= 4
            or not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= 32):
        raise ValueError("unbounded acceptance contract")
    for name, digest in artifacts.items():
        if not isinstance(name, str) or not re.fullmatch("[0-9a-f]{64}", str(digest)):
            raise ValueError("invalid artifact identity")
    ids = set()
    for target in coverage:
        if (set(target) != {"workstream_id", "revision", "session_id", "checks"}
                or not re.fullmatch("ws_[0-9a-f]{16}", target["workstream_id"])
                or target["workstream_id"] in ids or type(target["revision"]) is not int
                or target["revision"] < 1 or not isinstance(target["session_id"], str)
                or not isinstance(target["checks"], list) or not target["checks"]
                or len(set(target["checks"])) != len(target["checks"])
                or any(type(i) is not int or not 0 <= i < len(checks) for i in target["checks"])):
            raise ValueError("invalid exact work coverage")
        ids.add(target["workstream_id"])
    reports, commands = set(), set()
    for c in checks:
        if (set(c) != {"command", "report", "min_tests"} or type(c["min_tests"]) is not int
                or not 1 <= c["min_tests"] <= 100000 or not isinstance(c["command"], str)
                or not isinstance(c["report"], str) or c["report"] in artifacts
                or c["report"] in reports or c["command"] in commands
                or re.search(r"[;&|><`$\n\r]", c["command"])):
            raise ValueError("invalid check")
        argv = shlex.split(c["command"])
        if (len(argv) < 5 or argv[0].rsplit("/", 1)[-1] not in ("python", "python3")
                or argv[1:3] != ["-m", "pytest"]
                or argv.count("--junitxml=" + c["report"]) != 1
                or sum(a.startswith(("--junitxml", "--junit-xml")) for a in argv) != 1):
            raise ValueError("only explicit pytest + JUnit checks supported")
        reports.add(c["report"])
        commands.add(c["command"])
    return s


def finish(agent, goal, rows, result):
    """Owner result readback plus atomic exact-ID acceptance; no model API."""
    from agent.workstream_human_hooks import _authenticate
    from agent.workstream_procedures import _workflow
    from agent.work_lifecycle import is_silent_reply
    if goal is None:
        return None
    scope = goal.scope
    spec = _spec(goal.content)

    def current():
        binding = _authenticate(agent, role="user", message=goal.message,
            session_id=goal.session_id, message_id=None, row_id=rows[0]["id"])
        return (binding is not None and binding[2].ingress_event_id == goal.event_id
                and ws.scope_for_agent(agent) == scope and ws.load_settings().active
                and not getattr(agent, "_interrupt_requested", False)
                and 0 <= time.monotonic() - goal.started <= 300
                and _read(scope, goal.path, 8192) == goal.content)

    if (not current() or not result.get("completed") or is_silent_reply(result.get("final_response"))
            or not str(result.get("final_response") or "").strip()
            or any(result.get(k) for k in ("failed", "interrupted", "partial", "error", "cleanup_errors"))):
        return None
    if _workflow(rows, min_commands=1)[0] != "success":
        return None
    calls = [c for r in rows if r["role"] == "assistant" for c in (r.get("tool_calls") or [])]
    if [json.loads(c["function"]["arguments"])["command"] for c in calls] != [c["command"] for c in spec["checks"]]:
        return None
    for call in calls:
        args = json.loads(call["function"]["arguments"])
        if args.get("workdir") != scope.project:
            return None
    live = {m.get("tool_call_id"): m.get("content") for m in result.get("messages", []) if m.get("role") == "tool"}
    tool_rows = [r for r in rows if r["role"] == "tool"]
    if any(live.get(r.get("tool_call_id")) != r["content"] for r in tool_rows):
        return None
    reports = []
    for check in spec["checks"]:
        value = _read(scope, check["report"])
        if b"<!" in value:
            return None
        root = ElementTree.fromstring(value)
        cases = root.findall(".//testcase")
        if (len(cases) < check["min_tests"] or root.tag not in ("testsuites", "testsuite")
                or any(True for tag in ("failure", "error", "skipped") for _ in root.iter(tag))):
            return None
        reports.append(hashlib.sha256(value).hexdigest())
    def reports_current():
        return all(hashlib.sha256(_read(scope, check["report"])).hexdigest() == digest
                   for check, digest in zip(spec["checks"], reports))

    artifact_digest = _artifacts(scope, spec["artifacts"])
    proof = {"v": 1, "owner": "foreground_pytest", "spec_digest": hashlib.sha256(goal.content).hexdigest(),
             "artifact_digest": artifact_digest, "check_ids": _digest([
                 [(r["id"], r["tool_call_id"], r["content"]) for r in tool_rows], reports]),
             "event": goal.event_id, "row": rows[0]["id"]}
    source = "accept:" + _digest(proof)
    detail = json.dumps(proof, separators=(",", ":"))
    if len(detail) > 512:
        return None
    with np.write_batch():
        if not current() or not reports_current() or _artifacts(scope, spec["artifacts"]) != artifact_digest:
            raise ValueError("acceptance lost current ownership")
        for target, (wid, snapshot) in zip(spec["coverage"], goal.evidence):
            row = np.get_workstream(scope, wid)
            if (row is None or row["revision"] != target["revision"]
                    or row["status"] not in ("open", "deferred") or _evidence(scope, wid) != snapshot):
                raise ValueError("work changed since acceptance declaration")
            summary = "Candidate artifacts and declared checks verified; review/install/release not implied"
            np.add_receipt(scope, wid, kind="executed", owner="acceptance", source_id=source, detail=detail)
            np.add_evidence(scope, wid, source_kind="acceptance", source_id=source,
                status="verified", trust="owner_session", session_id=goal.session_id,
                summary=summary, key=proof["spec_digest"])
            np.update_workstream(scope, wid, expected_revision=row["revision"],
                                 status="resolved", summary=summary, actor="acceptance")
        if not current() or not reports_current() or _artifacts(scope, spec["artifacts"]) != artifact_digest:
            raise ValueError("acceptance changed during commit")
    # Exact target readback is required before success is exposed.
    if any(np.get_workstream(scope, t["workstream_id"])["status"] != "resolved" for t in spec["coverage"]):
        return None
    return {"status": "success", "execution_ids": [source], "drafts": [],
            "covered_work_ids": [t["workstream_id"] for t in spec["coverage"]]}
