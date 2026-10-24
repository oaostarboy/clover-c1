"""Same-interpreter PR-head/candidate checks; run under the documented bwrap.

All output and the exact baseline archive stay inside this worktree. No package
install, live Clover access, network, update command or service action is used.
"""
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
BASE = "bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e"
REVIEW = "9264b3989e515cadddaf73323680bd60399e14e6"
OUT = ROOT / "review/status-lock/receipts"
SANDBOX = ROOT / ".test-sandbox/status-lock"
ORIGINAL = "review/status-lock/original_test_status_security_concurrency.py"
NEW = "tests/gateway/test_status_lock_security.py"
OUT.mkdir(parents=True, exist_ok=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
# Exact original probes are deliberately retained, including the attack's
# obsolete 0644 precondition; results distinguish that from the fixed contract.
original_bytes = subprocess.check_output([
    "git", "show", f"{REVIEW}:review/independent-pr148/test_status_security_concurrency.py"], cwd=ROOT)
(ROOT / ORIGINAL).write_bytes(original_bytes)
base = SANDBOX / "baseline"
base.mkdir(exist_ok=True)
archive = subprocess.check_output(["git", "archive", BASE], cwd=ROOT)
with tarfile.open(fileobj=io.BytesIO(archive)) as source:
    source.extractall(base, filter="data")
for relative in (ORIGINAL, NEW):
    target = base / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / relative, target)
module = ast.parse((ROOT / "review/owner-attestation/run_checks.py").read_text())
files = next(ast.literal_eval(node.value) for node in module.body
             if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "FILES" for t in node.targets))
results = {"baseline": BASE, "review_object": REVIEW, "python": sys.executable,
           "python_version": sys.version, "isolation": "bwrap read-only host, writable own worktree, unshare-net; credential-free private HOME; socket deny plugin",
           "source_sha256": {}, "runs": []}
for name, root in (("baseline", base), ("candidate", ROOT)):
    results["source_sha256"][name] = hashlib.sha256((root / "gateway/status.py").read_bytes()).hexdigest()
    for index, file in enumerate([*files, NEW, ORIGINAL]):
        run = SANDBOX / f"{name}-{index}"
        for directory in ("home", "tmp", "clover"):
            (run / directory).mkdir(parents=True, exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(run / "home"),
               "TMPDIR": str(run / "tmp"), "CLOVER_HOME": str(run / "clover"),
               "TZ": "UTC", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_PLUGINS": "deny_network",
               "PYTHONPATH": os.pathsep.join([str(root / "review/owner-attestation"), str(root)])}
        junit = OUT / f"{name}-{index}.xml"
        cmd = [sys.executable, "-m", "pytest", file, "-q", "-s", "--tb=short",
               f"--basetemp={run / 'pytest'}", f"--junitxml={junit}"]
        if file.endswith("test_update_delivery_guard_wrappers.py"):
            # Preserve the original review's exact scope, including its two
            # process-signalling topology exclusions; no native proof claim.
            cmd.extend(["-k", "not test_real_stderr_wrapper_and_direct_worker_identity"])
        completed = subprocess.run(cmd, cwd=root, env=env, capture_output=True,
                                   text=True, timeout=180, umask=0o022)
        (OUT / f"{name}-{index}.log").write_text(completed.stdout + completed.stderr)
        cases = list(ET.parse(junit).getroot().iter("testcase")) if junit.exists() else []
        row = {"source": name, "file": file, "command": cmd,
               "exit_code": completed.returncode, "tests": len(cases),
               "passed": sum(not any(c.find(kind) is not None for kind in ("failure", "error", "skipped")) for c in cases),
               "skipped": [f"{c.get('classname')}::{c.get('name')}" for c in cases if c.find("skipped") is not None],
               "failures": [{"node": f"{c.get('classname')}::{c.get('name')}", "phase": kind}
                            for c in cases for kind in ("failure", "error") if c.find(kind) is not None]}
        results["runs"].append(row)
        (OUT / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps({k: v for k, v in row.items() if k != "command"}), flush=True)
checks = {}
for name in ("baseline", "candidate"):
    rows = [r for r in results["runs"] if r["source"] == name]
    focused = rows[:len(files)]
    checks[f"{name}_focus"] = (len(focused) == len(files)
        and all(r["exit_code"] == 0 and not r["failures"] for r in focused))
new_rows = {r["source"]: r for r in results["runs"] if r["file"] == NEW}
checks["candidate_new_contract"] = (new_rows["candidate"]["exit_code"] == 0
    and new_rows["candidate"]["tests"] == 17 and new_rows["candidate"]["passed"] == 17)
checks["baseline_new_contract_red"] = (new_rows["baseline"]["tests"] == 17
    and len(new_rows["baseline"]["failures"]) == 11
    and all(f["phase"] == "failure" for f in new_rows["baseline"]["failures"]))
original_rows = {r["source"]: r for r in results["runs"] if r["file"] == ORIGINAL}
checks["baseline_original_reproduces"] = (original_rows["baseline"]["tests"] == 5
    and {f["node"].split("::")[-1] for f in original_rows["baseline"]["failures"]} == {
        "test_read_only_lock_holder_cannot_disable_status", "test_lock_is_private_under_default_umask"}
    and "TimeoutError: runtime status writer is busy" in (OUT / "baseline-13.log").read_text())
checks["candidate_original_only_obsolete_precondition"] = (original_rows["candidate"]["passed"] == 4
    and [f["node"].split("::")[-1] for f in original_rows["candidate"]["failures"]] == [
        "test_read_only_lock_holder_cannot_disable_status"]
    and "assert stat.S_IMODE(lock.stat().st_mode) == 0o644" in (OUT / "candidate-13.log").read_text())
results["checks"] = checks
results["checks_passed"] = all(checks.values())
(OUT / "results.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps({"checks": checks, "checks_passed": results["checks_passed"]}))
print("Raw receipts:", OUT)
sys.exit(0 if results["checks_passed"] else 1)
