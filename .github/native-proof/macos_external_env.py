"""Disposable native macOS external-editable runtime proof; never run on fleet hosts.

No product mocks. CLI repair is real. Rollback uses the real provisioner/stager
and cutover, with a deliberate import failure installed in the candidate only.
The plist is executed as argv by this harness, NOT registered with launchd.
This is candidate-to-same-candidate runtime repair, not released-old-to-new proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
import traceback


def dump(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def native_guard():
    if platform.system() != "Darwin":
        raise RuntimeError("NOT_EXERCISED: this fixture requires native macOS")
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise RuntimeError("requires a disposable GitHub-hosted runner; never use Suni/Julia")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity():
    import sqlite3
    from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable
    with sqlite3.connect(":memory:") as conn:
        source_id = conn.execute("select sqlite_source_id()").fetchone()[0]
    return dict(os=platform.platform(), macos=platform.mac_ver(), machine=platform.machine(),
                python=sys.version, prefix=sys.prefix, base_prefix=sys.base_prefix,
                executable=sys.executable, sqlite=sqlite3.sqlite_version,
                sqlite_source_id=source_id,
                vulnerable=is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info))


def write_state(label):
    from clover_state import SessionDB
    from clover_cli import kanban_db
    from gateway.shutdown_flush import spool_dropped_transcript_message
    home = Path(os.environ["CLOVER_HOME"])
    db = SessionDB()
    try:
        db.create_session("mac-native-" + label, "cli")
        db.append_message("mac-native-" + label, "user", "message-" + label)
    finally:
        db.close()
    conn = kanban_db.connect(home / "kanban.db")
    try:
        task = kanban_db.create_task(conn, title="task-" + label, triage=True)
        conn.commit()
    finally:
        conn.close()
    pending = spool_dropped_transcript_message("mac-native-" + label,
                                              {"role": "user", "content": "pending-" + label})
    assert pending is not None
    return dict(label=label, task=task, pending=str(pending), pending_sha=sha(pending))


def verify_state(records):
    import sqlite3
    home = Path(os.environ["CLOVER_HOME"])
    results = []
    # Read existing owned databases without changing their journal mode.
    for rec in records:
        with sqlite3.connect((home / "state.db").as_uri() + "?mode=ro", uri=True) as conn:
            content = [r[0] for r in conn.execute("select content from messages where session_id=?",
                                                ("mac-native-" + rec["label"],))]
            assert "message-" + rec["label"] in content
            pending_in_db = "pending-" + rec["label"] in content
            mode = conn.execute("pragma journal_mode").fetchone()[0]
            assert conn.execute("pragma quick_check").fetchone()[0] == "ok"
        with sqlite3.connect((home / "kanban.db").as_uri() + "?mode=ro", uri=True) as conn:
            assert conn.execute("select title from tasks where id=?", (rec["task"],)).fetchone()[0] == "task-" + rec["label"]
            assert conn.execute("pragma quick_check").fetchone()[0] == "ok"
        retained = Path(rec["pending"]).is_file() and sha(rec["pending"]) == rec["pending_sha"]
        assert retained or pending_in_db, "pending record was lost"
        results.append(dict(label=rec["label"], messages=content, journal_mode=mode,
                            pending_retained=retained, pending_recovered=pending_in_db))
    return results


def child(args):
    native_guard()
    from clover_cli import managed_uv as m
    root, live = Path(args.source), Path(sys.prefix)
    if args.mode == "identity":
        dump(args.output, identity())
    elif args.mode == "state":
        dump(args.output, write_state(args.label))
    elif args.mode == "verify":
        dump(args.output, verify_state(json.loads(Path(args.records).read_text())))
    elif args.mode == "ownership":
        assert m._running_editable_venv(root) == live
        assert m._default_live_venv(root) == live
        # The venv's Python symlink may resolve to a shared base: prefix identity
        # remains the venv, not that base. A symlink of the venv is NOT authorized.
        alias = live.with_name("external-alias")
        alias.symlink_to(live, target_is_directory=True)
        result = m.repair_vulnerable_runtime(args.uv, project_root=root, venv_dir=alias)
        assert result.status == "refused"
        foreign = live.with_name("foreign")
        subprocess.run([args.uv, "venv", str(foreign), "--python", sys.executable,
                        "--no-python-downloads"], check=True)
        result2 = m.repair_vulnerable_runtime(args.uv, project_root=root, venv_dir=foreign)
        assert result2.status == "refused"
        from gateway.shutdown_flush import _fsync_directory
        directory_fsync = "supported"
        try:
            _fsync_directory(Path(os.environ["CLOVER_HOME"]))
        except OSError as exc:
            directory_fsync = f"unsupported errno={exc.errno}; no power-loss durability claim"
        dump(args.output, dict(selected=str(live), python_is_symlink=(live / "bin/python").is_symlink(),
                               resolved_python=str((live / "bin/python").resolve()),
                               alias_status=result.status, foreign_status=result2.status,
                               directory_fsync=directory_fsync))
    elif args.mode == "rollback":
        before = identity()
        assert before["vulnerable"], "historical seed runtime is not vulnerable; do not fabricate this case"
        old_stat = (live / "pyvenv.cfg").stat()
        current = m.probe_sqlite_runtime(live / "bin/python")
        assert current is not None
        lock = m._acquire_repair_lock(root / ".clover-runtime")
        assert lock is not None
        try:
            provisioned = m._install_safe_python_generation(args.uv, project_root=root, current=current)
            assert provisioned is not None, "real safe runtime provisioning failed"
            generation, python, info = provisioned
            candidate = m._stage_candidate_venv(args.uv, project_root=root, generation=generation, python=python)
            assert candidate is not None, "real locked candidate sync/import smoke failed"
            # Add a path-conditional import blocker to the candidate, not product
            # source. Staged smoke succeeds; promoted-path smoke really fails.
            site = Path(subprocess.check_output([str(candidate / "bin/python"), "-I", "-c",
                          "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
            (site / "macos_cutover_fault.py").write_text(
                "import sys\nclass Fault:\n def find_spec(self, fullname, path=None, target=None):\n"
                f"  if sys.prefix == {str(live)!r} and fullname == 'yaml':\n"
                "   raise ImportError('intentional native post-promotion smoke failure')\n"
                "sys.meta_path.insert(0, Fault())\n", encoding="utf-8")
            (site / "macos_cutover_fault.pth").write_text("import macos_cutover_fault\n", encoding="utf-8")
            assert m._smoke_candidate_venv(candidate)[0], "fault must not reject staging"
            # Newest messages/tasks/pending writes occur AFTER staging and BEFORE
            # failed promotion. Rollback must not restore stale database copies.
            newest = write_state("newest-before-rollback")
            dump(Path(args.output).with_suffix(".newest.json"), newest)
            ok, backup, final, detail = m._cut_over_candidate(candidate, project_root=root, live=live)
            assert not ok and backup is None, detail
            assert "intentional native post-promotion smoke failure" in detail, detail
            assert (live / "pyvenv.cfg").stat().st_ino == old_stat.st_ino
            after = m.probe_sqlite_runtime(live / "bin/python")
            assert after is not None
            assert after.sqlite_source_id == current.sqlite_source_id
            state = verify_state([newest])
            dump(args.output, dict(before=before, candidate_sqlite=info.sqlite_version_string,
                                   failed_cutover=detail, old_runtime_restored=True, newest=state,
                                   candidate_device=candidate.parent.stat().st_dev,
                                   live_device=live.parent.stat().st_dev))
        finally:
            m._release_repair_lock(lock)
    else:
        raise ValueError(args.mode)


def proof_support():
    return Path(__file__).with_name("proof.py")


def main(args):
    native_guard()
    for pin in (args.target_sha, args.target_tree):
        assert re.fullmatch("[0-9a-f]{40}", pin), "immutable SHA/tree required"
    evidence = Path(args.evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    scratch = Path(os.environ["RUNNER_TEMP"]) / ("mac-external-" + os.environ["GITHUB_RUN_ID"])
    scratch.mkdir()  # refuse adoption/reuse of any existing installation
    root, live, home = scratch / "source", scratch / "external environment", scratch / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k in {
        "PATH", "HOME", "TMPDIR", "RUNNER_TEMP", "GITHUB_ACTIONS", "RUNNER_ENVIRONMENT",
        "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    env.update(CLOVER_HOME=str(home), UV_PYTHON_INSTALL_DIR=str(scratch / "seed-python"),
               UV_CACHE_DIR=str(scratch / "uv-cache"), PYTHONUNBUFFERED="1")
    script = str(Path(__file__).resolve())
    uv = shutil.which("uv")
    assert uv
    commands = []

    def run(argv, name, *, cwd=None, allow_failure=False, timeout=1800):
        started = time.time()
        with (evidence / (name + ".log")).open("w") as log:
            try:
                result = subprocess.run([str(x) for x in argv], cwd=cwd or scratch, env=env,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
                rc = result.returncode
            except subprocess.TimeoutExpired:
                rc = 124
        commands.append(dict(argv=[str(x) for x in argv], exit=rc, started=started, ended=time.time()))
        dump(evidence / "commands.json", commands)
        if rc and not allow_failure:
            raise RuntimeError(f"{name} failed with {rc}; see {name}.log")
        return rc

    def call(mode, name, extra=()):
        return run([live / "bin/python", "-I", script, "--mode", mode,
                    "--source", root, "--uv", uv, "--output", evidence / name, *extra], name)

    run(["git", "clone", "--no-hardlinks", "--no-checkout", args.source, root], "clone")
    run(["git", "-C", root, "checkout", "--detach", args.target_sha], "pin")
    observed = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD", "HEAD^{tree}"], text=True).splitlines()
    assert observed == [args.target_sha, args.target_tree]
    dump(evidence / "pins.json", dict(target_sha=observed[0], target_tree=observed[1],
         workflow_sha=os.environ.get("GITHUB_SHA"), helper_sha256=sha(script),
         run_id=os.environ["GITHUB_RUN_ID"], native_os=platform.platform()))
    proof = proof_support()
    run([sys.executable, proof, "origin", "--repo", root, "--mirror", scratch / "origin.git",
         "--target", args.target_sha], "local-origin")
    (home / ".skip_upstream_prompt").touch()
    run([uv, "python", "install", "3.11.14", "--no-config"], "seed-python")
    run([uv, "venv", live, "--python", "3.11.14", "--managed-python", "--no-config"], "seed-venv")
    env["UV_PROJECT_ENVIRONMENT"] = str(live)
    env["VIRTUAL_ENV"] = str(live)
    run([uv, "sync", "--locked", "--extra", "all", "--python", live / "bin/python"], "seed-sync", cwd=root)
    env.pop("UV_PROJECT_ENVIRONMENT")
    env.pop("VIRTUAL_ENV")
    call("identity", "before.json")
    assert json.loads((evidence / "before.json").read_text())["vulnerable"]
    call("ownership", "ownership.json")
    call("state", "seed-state.json", ["--label", "seed"])
    # Explicitly no registered LaunchAgent. A launchd-shaped plist selects the
    # exact stable external CLI pathname, then this harness executes its argv.
    service_argv = [str(live / "bin/clover"), "gateway", "run"]
    plist = dict(Label="ai.clover.native-proof." + os.environ["GITHUB_RUN_ID"],
                 ProgramArguments=service_argv,
                 WorkingDirectory=str(scratch), EnvironmentVariables={"CLOVER_HOME": str(home)})
    (evidence / "isolated-service.plist").write_bytes(plistlib.dumps(plist))
    call("rollback", "rollback.json")
    records = [json.loads((evidence / "seed-state.json").read_text()),
               json.loads((evidence / "rollback.newest.json").read_text())]
    dump(evidence / "records.json", records)
    # No credentials, only a local health API with a non-secret fixture key.
    (home / ".env").write_text("API_SERVER_ENABLED=true\nAPI_SERVER_KEY=native-macos-fixture-only\nAPI_SERVER_PORT=18791\nAPI_SERVER_HOST=127.0.0.1\n")
    gateway_log = (evidence / "gateway.log").open("w")
    gateway = subprocess.Popen(service_argv, cwd=scratch, env=env,
                               stdout=gateway_log, stderr=subprocess.STDOUT)
    try:
        def observe(name, seconds):
            run([live / "bin/python", proof, "gateway", "--repo", root, "--home", home,
                 "--sha", args.target_sha, "--port", "18791", "--seconds", str(seconds),
                 "--output", evidence / name], name, timeout=300)
        observe("gateway-before.json", 0)
        began = time.time()
        run([live / "bin/clover", "update", "--yes"], "cli-update", cwd=scratch)
        receipt_path = home / "logs/update_receipts/latest.json"
        shutil.copy2(receipt_path, evidence / "update-receipt.json")
        sys.path.insert(0, str(proof.parent))
        import proof as proof_helpers
        proof_helpers.check_receipt(json.loads(receipt_path.read_text()), began, args.target_sha, args.target_sha)
        call("identity", "after.json")
        after = json.loads((evidence / "after.json").read_text())
        assert not after["vulnerable"]
        assert after["sqlite_source_id"] != json.loads((evidence / "before.json").read_text())["sqlite_source_id"]
        call("verify", "state-after-update.json", ["--records", evidence / "records.json"])
        # Owned harness restart is deliberate and recorded, NOT automatic
        # updater restart or actual launchd acceptance.
        if gateway.poll() is None:
            gateway.terminate()
            gateway.wait(timeout=90)
        gateway = subprocess.Popen(service_argv, cwd=scratch, env=env,
                                   stdout=gateway_log, stderr=subprocess.STDOUT)
        observe("gateway-after-owner-restart.json", 90)
        call("verify", "state-after-restart.json", ["--records", evidence / "records.json"])
        cli_version = [live / "bin/clover", "--version"]
        run(cli_version, "cli-after")
        backups = list(live.parent.glob(live.name + ".stale.runtime-*"))
        assert backups, "out-of-tree rollback backups must remain retained"
        dump(evidence / "result.json", dict(status="PASS_BOUNDED_NATIVE_RUNTIME_PROOF",
            backups=[str(x) for x in backups], target_sha=args.target_sha,
            automatic_restart="NOT_EXERCISED", actual_launchd="NOT_EXERCISED",
            real_chat_transport="NOT_EXERCISED", released_old_to_new="NOT_EXERCISED",
            in_flight_llm_turn="NOT_EXERCISED",
            note="Real CLI runtime repair; actual gateway; explicit owner harness restart; real failed cutover rollback."))
    finally:
        if gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=90)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait(timeout=10)
        gateway_log.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", default="main")
    p.add_argument("--source", required=True)
    for name in ["target-sha", "target-tree", "evidence", "uv", "output", "label", "records"]:
        p.add_argument("--" + name)
    a = p.parse_args()
    try:
        main(a) if a.mode == "main" else child(a)
    except Exception:
        if a.evidence:
            Path(a.evidence).mkdir(parents=True, exist_ok=True)
            dump(Path(a.evidence) / "failure.json", dict(status="FAIL_OR_NOT_EXERCISED", error=traceback.format_exc()))
        raise
