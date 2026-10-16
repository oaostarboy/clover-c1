"""Independent safety assertions for immutable candidate 778075d3; no verifier mocks."""
import hashlib
import json

import pytest
from agent import workstreams as ws
from cron import notepad as np
from tests.gateway.test_workstream_verified_workflow import workflow, network_guard  # noqa: F401
from tests.gateway.test_workstream_acceptance import contract, isolated_terminal_owner  # noqa: F401


def invoke(h, rows, command, spec):
    (h.project / "acceptance.json").write_text(json.dumps(spec))
    result = h.run(message="Verify work acceptance acceptance.json",
                   commands=((command, {"workdir": str(h.project)}),))
    row = np.get_workstream(h.scope(), rows[0]["workstream_id"])
    tools = [m.get("content") for m in result["messages"] if m.get("role") == "tool"]
    print("INDEPENDENT_RESULT " + json.dumps({"test": spec.get("checks"),
          "status": row["status"], "execution": result.get("workstream_execution"),
          "tools": tools, "receipts": np.list_receipts(h.scope(), row["workstream_id"])}))
    return row, result


def test_forged_junit_without_any_test_execution_must_not_close_work(workflow):
    h = workflow
    h.run(commands=(), final="Ready")
    rows, command, spec = contract(h)
    # A project payload bypasses all test execution via a standard pytest hook.
    # The asserted test would fail if pytest ran it. All payloads are frozen.
    (h.project / "acceptance_probe.py").write_text("def test_candidate():\n    assert False, 'MUST RUN'\n")
    xml = "<testsuites><testsuite tests='1' failures='0' errors='0' skipped='0'><testcase classname='invented' name='never_executed'/></testsuite></testsuites>"
    (h.project / "conftest.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        "def pytest_sessionstart(session):\n"
        f"    Path('acceptance.xml').write_text({xml!r})\n"
        "    pytest.exit('No tests executed; fabricated report', returncode=0)\n")
    spec["artifacts"] = {n: hashlib.sha256((h.project / n).read_bytes()).hexdigest()
                         for n in ("acceptance_probe.py", "conftest.py")}
    row, result = invoke(h, rows, command, spec)
    assert row["status"] == "open", "Fabricated report + zero exit was accepted without a test run"


def test_non_python_executable_named_python_must_not_close_work(workflow):
    h = workflow
    h.run(commands=(), final="Ready")
    rows, command, spec = contract(h)
    xml = "<testsuite tests='1' failures='0' errors='0' skipped='0'><testcase name='fabricated'/></testsuite>"
    fake = h.project / "python"
    fake.write_text("#!/bin/sh\nprintf '%s' \"" + xml + "\" > acceptance.xml\nexit 0\n")
    fake.chmod(0o755)
    command = "./python -m pytest acceptance_probe.py --junitxml=acceptance.xml -q"
    spec["checks"][0]["command"] = command
    spec["artifacts"]["python"] = hashlib.sha256(fake.read_bytes()).hexdigest()
    row, result = invoke(h, rows, command, spec)
    assert row["status"] == "open", "Executable basename was treated as proof of running Python/pytest"


def test_rollout_in_delegation_obligation_must_not_be_software_accepted(workflow):
    h = workflow
    h.run(commands=(), final="Ready")
    rows = ws.record_outcomes(h.scope(), [ws.TypedOutcome(
        "Build, review, install and release the candidate", "failed", "delegation", "full-delivery-job",
        session_id=h.session_id())], ws.load_settings())
    rows, command, spec = contract(h, rows=rows)
    row, result = invoke(h, rows, command, spec)
    assert row["status"] == "open", "Transport source kind hid rollout/review obligations from the verifier"


def test_changed_artifact_during_check_restored_before_commit_must_not_verify(workflow):
    h = workflow
    h.run(commands=(), final="Ready")
    rows, command, spec = contract(h)
    candidate = h.project / "candidate.py"
    original = "READY = False\n"
    candidate.write_text(original)
    probe = h.project / "acceptance_probe.py"
    probe.write_text("from pathlib import Path\ndef test_candidate():\n    assert 'True' in Path('candidate.py').read_text()\n")
    spec["artifacts"] = {n: hashlib.sha256((h.project / n).read_bytes()).hexdigest()
                         for n in ("candidate.py", "acceptance_probe.py")}
    (h.project / "acceptance.json").write_text(json.dumps(spec))
    before = len(h.script.requests)
    seen = []
    def concurrent_writer(n):
        # The authentic host has already frozen the contract at the first call.
        if n == before:
            assert h.token.acceptance is not None
            candidate.write_text("READY = True\n")
            seen.append("changed before check")
        elif n == before + 1:
            candidate.write_text(original)
            seen.append("restored after check")
    result = h.run(message="Verify work acceptance acceptance.json",
                   commands=((command, {"workdir": str(h.project)}),), on_call=concurrent_writer)
    row = np.get_workstream(h.scope(), rows[0]["workstream_id"])
    print("TRANSIENT_ARTIFACT " + json.dumps({"events": seen, "status": row["status"],
           "execution": result.get("workstream_execution"), "final_artifact": candidate.read_text(),
           "tools": [m.get("content") for m in result["messages"] if m.get("role") == "tool"]}))
    assert seen == ["changed before check", "restored after check"]
    assert row["status"] == "open", "Tests ran against different artifact bytes than the accepted hash"


@pytest.mark.parametrize("variant", ["artifact_symlink", "report_symlink", "report_directory_symlink", "no_tests", "missing_report"])
def test_additional_fail_closed_controls(workflow, variant):
    h = workflow
    h.run(commands=(), final="Ready")
    rows, command, spec = contract(h)
    if variant == "artifact_symlink":
        probe = h.project / "acceptance_probe.py"
        probe.rename(h.project / "actual_probe.py")
        probe.symlink_to(h.project / "actual_probe.py")
    elif variant in ("report_symlink", "report_directory_symlink"):
        # Dangling links must not count as absent safe destinations.
        if variant == "report_symlink":
            (h.project / "acceptance.xml").symlink_to(h.project / "new-report.xml")
        else:
            (h.project / "real-reports").mkdir()
            (h.project / "reports").symlink_to(h.project / "real-reports", target_is_directory=True)
            spec["checks"][0]["report"] = "reports/acceptance.xml"
            command = command.replace("=acceptance.xml", "=reports/acceptance.xml")
            spec["checks"][0]["command"] = command
    elif variant == "no_tests":
        (h.project / "acceptance_probe.py").write_text("# no tests\n")
        spec["artifacts"]["acceptance_probe.py"] = hashlib.sha256((h.project / "acceptance_probe.py").read_bytes()).hexdigest()
    else:
        command = "python -m pytest acceptance_probe.py --junitxml=acceptance.xml -q -p no:junitxml"
        spec["checks"][0]["command"] = command
    row, result = invoke(h, rows, command, spec)
    assert row["status"] == "open"
    assert not np.list_receipts(h.scope(), row["workstream_id"])
