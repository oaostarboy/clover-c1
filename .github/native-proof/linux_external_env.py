"""Native hosted Linux external-runtime failure proof.
Real dependencies and provision/cutover paths; no service/update claims in this row.
Intentional candidate-only import failure verifies rollback preserves new data.
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
    if platform.system() != "Linux":
        raise RuntimeError("NOT_EXERCISED: this fixture requires native Linux")
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise RuntimeError("requires a disposable GitHub-hosted runner; never use Suni/Julia")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity():
    import sqlite3
    from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable
    with sqlite3.connect(":memory:") as conn:
        source_id = conn.execute("select sqlite_source_id()").fetchone()[0]
    return dict(os=platform.platform(), linux=platform.release(), machine=platform.machine(),
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
    elif args.mode == "provision-failure":
        before = identity()
        assert before["vulnerable"]
        inode = (live / "pyvenv.cfg").stat().st_ino
        newest = write_state("newest-before-provision-failure")
        # Real executable fails real provisioning commands; no monkeypatch.
        failed = m.repair_vulnerable_runtime("/usr/bin/false", project_root=root, venv_dir=live)
        assert failed.status == "failed", failed
        assert (live / "pyvenv.cfg").stat().st_ino == inode
        state = verify_state([newest])
        dump(args.output, dict(status=failed.status, detail=failed.detail,
                              preserved=state, before=before))
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



def main(args):
    native_guard()
    evidence=Path(args.evidence); evidence.mkdir(parents=True,exist_ok=True)
    scratch=Path(os.environ["RUNNER_TEMP"])/"runtime-failure"
    scratch.mkdir(); home=scratch/"home";home.mkdir()
    root=scratch/"source";live=scratch/"external"
    env=dict(os.environ,CLOVER_HOME=str(home),UV_PYTHON_INSTALL_DIR=str(scratch/"python"))
    env.pop("PYTHONPATH",None);env.pop("VIRTUAL_ENV",None)
    uv=shutil.which("uv");commands=[]
    def run(argv,name,cwd=scratch,allow=False):
        with (evidence/(name+".log")).open("w") as log:
            proc=subprocess.run(list(map(str,argv)),cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=1500)
        commands.append(dict(argv=list(map(str,argv)),exit=proc.returncode));dump(evidence/"commands.json",commands)
        if proc.returncode and not allow:raise RuntimeError(name+" failed")
        return proc.returncode
    run(["git","clone","--no-hardlinks",str(Path(args.source).resolve()),root],"clone")
    run(["git","-C",root,"fetch","https://github.com/oaostarboy/clover-c1.git",args.target_sha],"candidate-objects")
    run(["git","-C",root,"checkout","--detach",args.target_sha],"pin")
    assert subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD^{tree}"],text=True).strip()==args.target_tree
    run([uv,"python","install","3.11.14"],"seed-python")
    run([uv,"venv",live,"--python","3.11.14","--managed-python"],"seed-venv")
    env["UV_PROJECT_ENVIRONMENT"]=str(live)
    run([uv,"sync","--locked","--extra","all","--python",live/"bin/python"],"seed-sync",cwd=root)
    env.pop("UV_PROJECT_ENVIRONMENT")
    def call(mode,name,extra=()):return run([live/"bin/python","-I",Path(__file__).resolve(),"--mode",mode,"--source",root,"--uv",uv,"--output",evidence/name,*extra],name)
    call("identity","before.json");call("ownership","ownership.json");call("state","seed.json",["--label","seed"])
    call("provision-failure","provision-failure.json")
    call("rollback","rollback.json")
    records=[json.loads((evidence/"seed.json").read_text()),json.loads((evidence/"rollback.newest.json").read_text())]
    dump(evidence/"records.json",records)
    call("verify","preserved.json",["--records",evidence/"records.json"])
    dump(evidence/"result.json",dict(status="PASS_REAL_PROVISION_AND_FAILED_CUTOVER_ROLLBACK",target=args.target_sha,workflow=os.environ["GITHUB_SHA"],native_os=platform.platform(),service="NOT_EXERCISED_IN_THIS_ROW",test_count=4))

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--mode",default="main");p.add_argument("--source",required=True)
    for name in ["target-sha","target-tree","evidence","uv","output","label","records"]:p.add_argument("--"+name)
    a=p.parse_args()
    try:main(a) if a.mode=="main" else child(a)
    except Exception:
        if a.evidence:
            Path(a.evidence).mkdir(parents=True,exist_ok=True);dump(Path(a.evidence)/"failure.json",dict(error=traceback.format_exc()))
        raise
