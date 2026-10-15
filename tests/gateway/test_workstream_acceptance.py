"""Authenticated host -> real pytest -> exact accepted work -> durable capsule."""
import hashlib
import json

from agent import workstreams as ws
from cron import notepad as np
from tests.gateway.test_workstream_verified_workflow import workflow, network_guard  # noqa: F401


def contract(h, *, covers=None):
    # These are accepted tasks, not planted verification or provenance receipts.
    scope = h.scope()
    sid = h.session_id()
    rows = ws.record_outcomes(scope, [
        ws.TypedOutcome("Build and test candidate", "failed", "delegation", "original", session_id=sid),
        ws.TypedOutcome("Review and release candidate", "not_run", "rollout", "release", session_id=sid),
    ], ws.load_settings())
    probe = h.project / "acceptance_probe.py"
    probe.write_text("def test_candidate():\n    assert 2 + 2 == 4\n")
    command = "python -m pytest acceptance_probe.py --junitxml=acceptance.xml -q"
    spec = {
        "version": 1,
        "coverage": [{"workstream_id": rows[0]["workstream_id"], "revision": rows[0]["revision"],
                      "session_id": sid, "checks": [0]}] if covers is None else covers,
        "artifacts": {"acceptance_probe.py": hashlib.sha256(probe.read_bytes()).hexdigest()},
        "checks": [{"command": command, "report": "acceptance.xml", "min_tests": 1}],
    }
    (h.project / "acceptance.json").write_text(json.dumps(spec))
    return rows, command, spec


def test_real_owner_checks_close_only_exact_covered_work(workflow):
    h = workflow
    # Establish a real authenticated session before accepting work in it.
    h.run(commands=(), final="Ready")
    rows, command, _ = contract(h)
    result = h.run(message="Verify work acceptance acceptance.json", commands=(command,))
    current = np.get_workstream(h.scope(), rows[0]["workstream_id"])
    assert current["status"] == "resolved"
    assert "verified" in current["summary"] and "unverified" not in current["summary"]
    assert np.get_workstream(h.scope(), rows[1]["workstream_id"]) == rows[1]
    evidence = np.list_evidence(h.scope(), current["workstream_id"])
    assert any(e["source_id"] == "original" and e["status"] == "failed" for e in evidence)
    accepted = [e for e in evidence if e["source_kind"] == "acceptance" and e["status"] == "verified"]
    assert len(accepted) == 1
    assert accepted[0]["trust"] == "owner_session"
    receipts = np.list_receipts(h.scope(), current["workstream_id"])
    assert len(receipts) == 1
    proof = json.loads(receipts[0]["detail"])
    assert proof["check_ids"] and proof["artifact_digest"] and proof["spec_digest"]
    assert result["workstream_execution"]["status"] == "success"
    # A fresh read, not instance lineage, removes the completed warning and
    # keeps the separate review/install/release work visible.
    text = ws.thread_context(h.scope(), ws.load_settings())
    assert "Build and test candidate" not in text
    assert "Review and release candidate" in text
