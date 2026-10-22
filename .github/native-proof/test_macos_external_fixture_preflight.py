"""Linux-safe verifier preflight only. These tests are NOT native acceptance."""
import ast

import json
from pathlib import Path
import platform
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).with_name("macos_external_env.py")
WORKFLOW = SCRIPT.parents[1] / "workflows/macos-external-runtime-proof.yml"



def test_fixture_compiles():
    compile(SCRIPT.read_text(), str(SCRIPT), "exec")


def test_help_executes_without_product_or_native_side_effects():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--target-sha" in result.stdout


def test_non_macos_is_refused_before_source_or_install_access(tmp_path):
    if platform.system() == "Darwin":
        pytest.skip("this preflight specifically checks the real non-macOS refusal")
    out = tmp_path / "evidence"
    result = subprocess.run([sys.executable, str(SCRIPT), "--source", str(tmp_path / "must-not-exist"),
                             "--target-sha", "0" * 40, "--target-tree", "0" * 40,
                             "--evidence", str(out)], capture_output=True, text=True)
    assert result.returncode == 1
    data = json.loads((out / "failure.json").read_text())
    assert data["status"] == "FAIL_OR_NOT_EXERCISED"
    assert "requires native macOS" in data["error"]
    assert not (out / "result.json").exists()
    assert not (tmp_path / "must-not-exist").exists()


def test_no_platform_spoof_or_product_mock_boundary():
    tree = ast.parse(SCRIPT.read_text())
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any("mock" in name for name in imports)
    # Product calls are actual imports, not injected replacement implementations.
    assert "m._cut_over_candidate(candidate" in SCRIPT.read_text()
    assert '"update", "--yes"' in SCRIPT.read_text()


def test_workflow_is_bounded_to_hosted_macos_and_test_branch():
    import yaml
    doc = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert doc["jobs"]["native"]["runs-on"] == "macos-latest"
    assert doc["jobs"]["native"]["timeout-minutes"] == "75"
    assert doc["permissions"] == {"contents": "read"}
    assert doc["on"]["push"]["branches"] == ["ci/macos-external-runtime-proof/**"]
    assert doc["on"]["workflow_dispatch"]["inputs"]["target_sha"]["required"] == "true"


def test_state_assertions_preserve_newest_rows_and_pending_content():
    text = SCRIPT.read_text()
    assert 'write_state("newest-before-rollback")' in text
    assert 'pending_retained=retained, pending_recovered=pending_in_db' in text
    assert 'sqlite_source_id == current.sqlite_source_id' in text
    assert 'automatic_restart="NOT_EXERCISED"' in text
    assert 'actual_launchd="NOT_EXERCISED"' in text
