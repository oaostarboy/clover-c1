"""Linux regression: the gateway fixture must not trust shell Python selection."""
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from cron import notepad as np
from tests.gateway.test_workstream_acceptance import (
    contract, isolated_terminal_owner, network_guard, workflow,  # noqa: F401
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux shell/interpreter regression")


def python_without_site_packages(path):
    # Run a real other CPython with site packages disabled. This also works
    # when a developer's base interpreter happens to have pytest installed.
    path.write_text("#!/bin/sh\nexec " + shlex.quote(getattr(sys, "_base_executable")) + ' -S "$@"\n')
    path.chmod(0o700)


def test_fixture_uses_test_interpreter_after_real_shell_selects_foreign_python(workflow):
    h = workflow
    # A real base interpreter runs without dev extras. A private profile makes
    # shell selection deterministic without patching the terminal or runner.
    foreign = h.project / "foreign bin"
    foreign.mkdir()
    python_without_site_packages(foreign / "python")
    profile = Path(os.environ["HOME"]) / ".bash_profile"
    profile.write_text(f"export PATH={shlex.quote(str(foreign))}:$PATH\n")
    probe = (
        "import importlib.util,json,os,sys; from pathlib import Path; "
        "Path('interpreter-context.json').write_text(json.dumps({"
        "'argv':sys.argv,'executable':sys.executable,'prefix':sys.prefix,"
        "'pytest':str(importlib.util.find_spec('pytest')),'PATH':os.environ['PATH'],"
        "'cwd':os.getcwd()}))"
    )
    h.run(commands=(("python -c " + shlex.quote(probe), {"workdir": str(h.project)}),))
    context = json.loads((h.project / "interpreter-context.json").read_text())
    print("FOREIGN_INTERPRETER_CONTEXT " + json.dumps(context, sort_keys=True))
    assert Path(context["executable"]).resolve() == Path(getattr(sys, "_base_executable")).resolve()
    assert context["PATH"].split(os.pathsep)[0] == str(foreign)
    assert context["pytest"] == "None"
    rows, command, _ = contract(h)
    assert shlex.split(command)[0] == sys.executable
    result = h.run(message="Verify work acceptance acceptance.json",
                   commands=((command, {"workdir": str(h.project)}),))
    assert result["workstream_execution"]["status"] == "unverified"
    assert np.get_workstream(h.scope(), rows[0]["workstream_id"])["status"] == "open"
    assert not np.list_receipts(h.scope())
    assert not any(e["status"] == "verified" for e in np.list_scope_evidence(h.scope()))


@pytest.mark.parametrize("recognized_name", [True, False], ids=["missing-pytest", "foreign-interpreter"])
def test_foreign_interpreter_cannot_accept_work(workflow, recognized_name):
    h = workflow
    h.run(commands=(), final="Ready")
    foreign = h.project / "foreign bin"
    foreign.mkdir()
    # Exercise actual failed Python execution, not a planted XML/result. The
    # first path is valid contract syntax but lacks pytest; the second also
    # fails the unchanged explicit-interpreter name criterion.
    executable = foreign / ("python" if recognized_name else "foreign-python")
    python_without_site_packages(executable)
    rows, command, spec = contract(h)
    argv = shlex.split(command)
    argv[0] = str(executable)
    command = shlex.join(argv)
    spec["checks"][0]["command"] = command
    (h.project / "acceptance.json").write_text(json.dumps(spec))
    result = h.run(message="Verify work acceptance acceptance.json",
                   commands=((command, {"workdir": str(h.project)}),))
    tools = [json.loads(m["content"]) for m in result["messages"] if m.get("role") == "tool"]
    assert tools and tools[-1]["exit_code"] == 1
    assert "No module named pytest" in tools[-1]["output"]
    assert result["workstream_execution"]["status"] == ("unknown" if recognized_name else "failed")
    assert np.get_workstream(h.scope(), rows[0]["workstream_id"])["status"] == "open"
    assert not np.list_receipts(h.scope())
    assert not any(e["status"] == "verified" for e in np.list_scope_evidence(h.scope()))
