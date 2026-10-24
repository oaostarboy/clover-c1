"""Run bounded per-file checks with clean credentials/home and Python egress denial.

Usage: <test-python> review/owner-attestation/run_checks.py [source-root]
Reads the chosen Python environment; never installs into it. All test artifacts
stay beneath the source root. No gateway is launched by this runner.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

FILES = [
    "tests/gateway/test_status_update_owner.py",
    "tests/clover_cli/test_postpull_missing_owner_guard.py",
    "tests/gateway/test_status.py",
    "tests/gateway/test_runtime_status_liveness_reconciliation.py",
    "tests/gateway/test_adapter_connect_classification.py",
    "tests/gateway/test_multiplex_lifecycle.py",
    "tests/gateway/test_restart_reply_delivery.py",
    "tests/gateway/test_update_cron_drain.py",
    "tests/clover_cli/test_update_delivery_guard.py",
    "tests/clover_cli/test_update_delivery_guard_wrappers.py",
    "tests/clover_cli/test_legacy_successor_identity_fences.py",
    "tests/clover_cli/test_windows_update_single_relaunch.py",
]
root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
artifacts = root / ".test-sandbox" / "checks"
artifacts.mkdir(parents=True, exist_ok=True)
for directory in ("home", "tmp", "clover"):
    (artifacts / directory).mkdir(exist_ok=True)
env = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": str(artifacts / "home"),
    "TMPDIR": str(artifacts / "tmp"),
    "CLOVER_HOME": str(artifacts / "clover"),
    "TZ": "UTC", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": str(root / "review" / "owner-attestation"),
    "PYTEST_PLUGINS": "deny_network",
}
results = []
for index, file in enumerate(FILES):
    junit = artifacts / f"{index}.xml"
    cmd = [sys.executable, "-m", "pytest", file, "-q", "--tb=short",
           f"--basetemp={artifacts / ('pytest-' + str(index))}", f"--junitxml={junit}"]
    if file.endswith("test_update_delivery_guard_wrappers.py"):
        # Its two existing live-topology tests signal their disposable worker
        # processes on cleanup. This task permits no process signalling.
        cmd.extend(["-k", "not test_real_stderr_wrapper_and_direct_worker_identity"])
    process = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)
    (artifacts / f"{index}.log").write_text(process.stdout + process.stderr)
    cases = list(ET.parse(junit).getroot().iter("testcase")) if junit.exists() else []
    failures = [f"{c.get('classname')}::{c.get('name')}" for c in cases
                if c.find("failure") is not None or c.find("error") is not None]
    skipped = sum(c.find("skipped") is not None for c in cases)
    row = {"file": file, "exit_code": process.returncode, "tests": len(cases),
           "skipped": skipped, "failures": failures}
    results.append(row)
    print(json.dumps(row), flush=True)
summary = {"source_root": str(root), "files": results,
           "tests": sum(r["tests"] for r in results),
           "skipped": sum(r["skipped"] for r in results),
           "failed": sum(len(r["failures"]) for r in results),
           "file_errors": sum(r["exit_code"] not in (0, 1) for r in results)}
(artifacts / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps({k: v for k, v in summary.items() if k != "files"}))
sys.exit(1 if any(r["exit_code"] for r in results) else 0)
