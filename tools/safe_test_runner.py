#!/usr/bin/env python3
"""Run tests in credential-free unprivileged user/net/PID/IPC namespaces."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / ".artifacts/safe-runner"
RESULTS = ART / "results"
BWRAP = shutil.which("bwrap") or "/usr/sbin/bwrap"
UNSHARE = shutil.which("unshare") or "/usr/sbin/unshare"
VENV = pathlib.Path("/home/starboy/agents/clover-c1/venv")
PYTHON = pathlib.Path(sys.executable).resolve()
RUNTIME = PYTHON.parent.parent
SITE = VENV / "lib/python3.13/site-packages"
CRED_RE = re.compile(r"(KEY|TOKEN|PASS|SECRET|CREDENTIAL)", re.I)
GATEWAY_SHA = hashlib.sha256((ROOT / "gateway/run.py").read_bytes()).hexdigest()
RUNNER_SHA = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
RUN_ID = f"{time.time_ns():x}"
SAFE_ENV = {
    "HOME": "/tmp/private-home",
    "CLOVER_HOME": "/tmp/private-home/.clover",
    "CLOVER_TEST_ISOLATION": "/tmp/private-home/.clover",
    "TMPDIR": "/tmp/private-tmp",
    "PATH": "/usr/bin:/bin",
    "PYTHONPATH": "/work/src:/opt/site-packages:/results",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PWD": "/tmp/private-tmp",
    "PYTEST_PLUGINS": "safe_env_guard",
    "RUNNER_EXPECTED_GATEWAY_SHA256": GATEWAY_SHA,
}


def record(name: str, payload: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def tracked_diff_sha256() -> str:
    diff = subprocess.run(["git", "-C", str(ROOT), "diff", "--binary", "HEAD"], capture_output=True, check=True).stdout
    return hashlib.sha256(diff).hexdigest()


def bwrap_prefix() -> list[str]:
    args = [
        BWRAP, "--die-with-parent", "--new-session",
        "--unshare-user", "--uid", str(os.getuid()), "--gid", str(os.getgid()),
        "--unshare-net", "--unshare-pid", "--unshare-ipc",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", str(RUNTIME), str(RUNTIME),
        "--ro-bind", str(SITE), "/opt/site-packages",
        "--ro-bind", str(ROOT), "/work/src",
        "--bind", str(RESULTS), "/results",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--dir", "/tmp/private-home", "--dir", "/tmp/private-home/.clover",
        "--dir", "/tmp/private-tmp", "--dir", "/opt", "--dir", "/work",
        "--chdir", "/tmp/private-tmp", "--clearenv",
    ]
    for key, value in SAFE_ENV.items():
        args += ["--setenv", key, value]
    return args


def invoke(argv: list[str], timeout: int = 1200) -> subprocess.CompletedProcess:
    # Do not pass the agent/gateway environment. The whitelist above is rebuilt
    # for every child; `nice` is applied to test workers only.
    clean_host_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    # Each bwrap invocation owns a fresh network namespace. Preflight's ioctl
    # cannot bring loopback up for later pytest children, so do it in every
    # child before running either the preflight or pytest bootstrap.
    if len(argv) >= 2 and argv[0] == "-c":
        loopback = (
            "import fcntl,socket,struct; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
            "i=struct.pack('16sH',b'lo',0); f=struct.unpack('16sH',fcntl.ioctl(s.fileno(),0x8913,i))[1]; "
            "fcntl.ioctl(s.fileno(),0x8914,struct.pack('16sH',b'lo',f|1)) if not f&1 else None; s.close(); "
        )
        argv = ["-c", loopback + argv[1]]
    return subprocess.run(
        bwrap_prefix() + [str(PYTHON), "-S", *argv], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=timeout, env=clean_host_env, preexec_fn=lambda: os.nice(10),
    )


def preflight() -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    # Keep this harmless sentinel outside every bind-mounted input/runtime
    # directory. Never inspect or copy credential files.
    sentinel = pathlib.Path("/tmp") / f"clover-safe-sentinel-{os.getpid()}-{time.monotonic_ns()}.txt"
    sentinel.write_text("synthetic harmless sandbox sentinel\n")
    status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "-uall"], capture_output=True, text=True, check=True).stdout.splitlines()
    untracked_names = [line[3:] for line in status if line.startswith("?? ")]
    unsafe_untracked = [name for name in untracked_names if CRED_RE.search(pathlib.Path(name).name)]
    if unsafe_untracked:
        sentinel.unlink(missing_ok=True)
        raise RuntimeError("refusing untracked credential-named source inputs: " + repr(unsafe_untracked))
    host_netns = os.stat("/proc/self/ns/net").st_ino
    sentinel_guest_path = str(sentinel)
    code = r'''import errno, fcntl, hashlib, importlib.util, json, os, re, socket, struct, sys, threading
initial_env=sorted(os.environ)
cred=[k for k in os.environ if re.search(r"(KEY|TOKEN|PASS|SECRET|CREDENTIAL)",k,re.I)]
allowed={'HOME','CLOVER_HOME','CLOVER_TEST_ISOLATION','TMPDIR','PATH','PYTHONPATH','PYTHONDONTWRITEBYTECODE','PYTHONNOUSERSITE','LANG','LC_ALL','RUNNER_EXPECTED_GATEWAY_SHA256','PYTEST_PLUGINS','PWD'}
assert set(os.environ) <= allowed, sorted(set(os.environ)-allowed)
assert set(os.environ) >= allowed-{'PWD'}, 'explicit safe environment is incomplete'
assert not cred, 'credential-name environment variables leaked: '+repr(cred)
# Verify candidate wins from neutral cwd and the exact production module hash.
import gateway.run
module_path=os.path.realpath(gateway.run.__file__)
module_sha=hashlib.sha256(open(module_path,'rb').read()).hexdigest()
generated_env_names=sorted(os.environ)
expected_env=EXPECTED_ENV
os.environ.clear(); os.environ.update(expected_env)
post_whitelist_cred=[k for k in os.environ if re.search(r"(KEY|TOKEN|PASS|SECRET|CREDENTIAL)",k,re.I)]
assert not post_whitelist_cred and sorted(os.environ)==sorted(expected_env)
assert module_path == '/work/src/gateway/run.py', module_path
assert module_sha == os.environ['RUNNER_EXPECTED_GATEWAY_SHA256'], (module_sha,os.environ['RUNNER_EXPECTED_GATEWAY_SHA256'])
assert importlib.util.find_spec('pytest') is not None
assert importlib.util.find_spec('pytest_asyncio') is not None
# Bring private loopback up; no host interfaces/default route exist here.
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); ifr=struct.pack('16sH',b'lo',0)
flags=struct.unpack('16sH',fcntl.ioctl(s.fileno(),0x8913,ifr))[1]
if not flags & 1: fcntl.ioctl(s.fileno(),0x8914,struct.pack('16sH',b'lo',flags|1))
s.close()
server=socket.socket(); server.bind(('127.0.0.1',0)); server.listen(1); port=server.getsockname()[1]
def serve():
 c,_=server.accept(); data=c.recv(32); c.sendall(b'loopback-ok:'+data); c.close()
t=threading.Thread(target=serve); t.start(); c=socket.create_connection(('127.0.0.1',port),2); c.sendall(b'probe'); loop=c.recv(64).decode(); c.close(); t.join(); server.close(); assert loop=='loopback-ok:probe'
try: open(SENTINEL).read()
except OSError as e: sentinel={'inaccessible':True,'errno':e.errno}
else: raise AssertionError('host-only sentinel path unexpectedly visible')
routes=open('/proc/net/route').read().strip().splitlines()
interfaces=[x.split(':',1)[0].strip() for x in open('/proc/net/dev').read().splitlines()[2:] if ':' in x]
# No host runtime/DBus/docker sockets or host /tmp entries are mounted.
sockets={p:os.path.exists(p) for p in ['/run/user/1000','/run/docker.sock','/var/run/docker.sock','/run/dbus/system_bus_socket']}
assert not any(sockets.values()), sockets
x=socket.socket(); x.settimeout(2)
try: x.connect(('192.0.2.1',9)); ext='unexpected-connect'
except OSError as e: ext={'errno':e.errno,'name':errno.errorcode.get(e.errno)}
x.close(); assert isinstance(ext,dict) and ext['name']=='ENETUNREACH', ext
print(json.dumps({'initial_env_names':initial_env,'module_generated_env_names':generated_env_names,'post_whitelist_env_names':sorted(os.environ),'post_whitelist_credential_named_env':post_whitelist_cred,'env_names':sorted(os.environ),'credential_named_env':cred,'netns_inode':os.stat('/proc/self/ns/net').st_ino,'interfaces':interfaces,'routes':routes,'loopback':loop,'external_testnet_connect':ext,'sentinel':sentinel,'host_sockets_visible':sockets,'uid':os.getuid(),'gid':os.getgid(),'pidns_inode':os.stat('/proc/self/ns/pid').st_ino,'ipcns_inode':os.stat('/proc/self/ns/ipc').st_ino,'python_executable':sys.executable,'python_runtime_ready':True,'pytest_ready':True,'module_path':module_path,'module_sha256':module_sha}))'''.replace("SENTINEL", repr(sentinel_guest_path)).replace("EXPECTED_ENV", repr(SAFE_ENV))
    run = invoke(["-c", code], timeout=45)
    lines = run.stdout.strip().splitlines()
    data = json.loads(lines[-1]) if run.returncode == 0 and lines else {"output": run.stdout[-4000:]}
    data.update({
        "exit_code": run.returncode,
        "host_netns_inode": host_netns,
        "distinct_netns": data.get("netns_inode") != host_netns,
        "bwrap": BWRAP,
        "unshare": UNSHARE,
        "host_uid": os.getuid(),
        "candidate_root": str(ROOT),
        "candidate_gateway_run_sha256": GATEWAY_SHA,
        "runner_sha256": RUNNER_SHA,
        "candidate_git_head": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
        "tracked_diff_sha256": tracked_diff_sha256(),
        "run_id": RUN_ID,
        "untracked_path_names_audited": untracked_names,
        "credential_named_untracked_paths": unsafe_untracked,
        "runtime": str(RUNTIME),
        "site_packages": str(SITE),
        "mount_policy": "RO /usr,/bin,/lib,/lib64,Python runtime,venv site-packages,candidate; writable results + tmpfs /tmp; no host /home,/run,/var/run,/etc,/tmp or service sockets; minimal /dev",
    })
    data["preflight_ok"] = bool(
        run.returncode == 0 and data.get("distinct_netns")
        and data.get("loopback") == "loopback-ok:probe"
        and data.get("sentinel", {}).get("inaccessible")
        and data.get("external_testnet_connect", {}).get("name") == "ENETUNREACH"
        and not data.get("credential_named_env")
        and not data.get("post_whitelist_credential_named_env")
        and data.get("initial_env_names") == sorted(SAFE_ENV)
        and data.get("post_whitelist_env_names") == sorted(SAFE_ENV)
        and data.get("module_sha256") == GATEWAY_SHA
    )
    record("PREFLIGHT.json", data)
    (RESULTS / "PREFLIGHT.log").write_text(run.stdout)
    sentinel.unlink(missing_ok=True)
    return data


def run_file(path: str, timeout: int = 1200) -> dict:
    source = path if path.startswith("/work/src/") else "/work/src/" + path
    base, separator, node = source.partition("::")
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_")[-80:]
    slug = f"{stem}-{hashlib.sha256((source + chr(0) + (node if separator else '')).encode()).hexdigest()[:12]}-{RUN_ID}"
    junit = f"/results/{slug}.junit.xml"
    target = base + ("::" + node if separator else "")
    argv = ["-m", "pytest", "--basetemp=/tmp/pytest-tmp", "-o", "cache_dir=/tmp/pytest-cache", f"--junitxml={junit}", target]
    guard = """import json, os, re
ALLOWED = set(SAFE_ENV_NAMES) | {"PYTEST_VERSION", "PYTEST_CURRENT_TEST"}
PHASE_PATH = None
CRED = re.compile(r"(KEY|TOKEN|PASS|SECRET|CREDENTIAL)", re.I)
def _check():
    bad = sorted(k for k in os.environ if CRED.search(k))
    extra = sorted(set(os.environ) - ALLOWED)
    assert not bad and not extra, {"credential_names": bad, "extra_names": extra}
def pytest_sessionstart(session):
    global PHASE_PATH
    _check()
    xml_path = getattr(session.config.option, "xmlpath", None)
    PHASE_PATH = xml_path.replace(".junit.xml", ".phases.jsonl") if xml_path else None
    if PHASE_PATH:
        open(PHASE_PATH, "w").close()
def pytest_collection_finish(session): _check()
def pytest_runtest_logreport(report):
    if PHASE_PATH:
        with open(PHASE_PATH, "a") as stream:
            stream.write(json.dumps({"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome, "duration": report.duration}) + "\\n")
""".replace("SAFE_ENV_NAMES", repr(sorted(SAFE_ENV)))
    guard_tmp = RESULTS / f"safe_env_guard.{os.getpid()}.{threading.get_ident()}.tmp"
    guard_tmp.write_text(guard)
    guard_tmp.replace(RESULTS / "safe_env_guard.py")
    bootstrap = (
        "import gateway.run\nimport os, runpy, sys\n"
        f"allowed = {SAFE_ENV!r}\n"
        "os.environ.clear(); os.environ.update(allowed)\n"
        f"sys.argv = ['pytest', *{argv[2:]!r}]\n"
        "runpy.run_module('pytest', run_name='__main__')\n"
    )
    started = time.time()
    try:
        run = invoke(["-c", bootstrap], timeout=timeout)
        code = run.returncode
        output = run.stdout
    except subprocess.TimeoutExpired as e:
        code = 124
        partial = e.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        output = partial + "\nRUNNER_TIMEOUT\n"
    log_path = RESULTS / f"{slug}.log"
    log_path.write_text(output + f"\nEXIT_CODE={code}\n")
    cases = []
    junit_host = RESULTS / f"{slug}.junit.xml"
    if junit_host.exists():
        try:
            tree = ET.parse(junit_host)
            for case in tree.iter("testcase"):
                phases = []
                for child in list(case):
                    if child.tag in {"failure", "error", "skipped"}:
                        phases.append(child.tag)
                cases.append({"nodeid": case.attrib.get("name", ""), "classname": case.attrib.get("classname", ""), "phase_outcomes": phases or ["passed"], "duration": float(case.attrib.get("time", "0"))})
        except (ET.ParseError, OSError):
            cases = []
    phase_host = RESULTS / f"{slug}.phases.jsonl"
    phase_events = []
    if phase_host.exists():
        for line in phase_host.read_text().splitlines():
            if line.strip():
                phase_events.append(json.loads(line))
    result = {"file": source, "argv": argv, "exit_code": code, "elapsed_seconds": round(time.time()-started, 3), "source_sha256": GATEWAY_SHA, "runner_sha256": RUNNER_SHA, "candidate_git_head": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(), "tracked_diff_sha256": tracked_diff_sha256(), "run_id": RUN_ID, "junit": str(junit_host), "log": str(log_path), "phase_events_file": str(phase_host), "phase_events": phase_events, "testcases": cases, "output_tail": output[-3000:]}
    record(f"{slug}.json", result)
    return result


def run_group(paths: list[str], timeout: int = 1200) -> dict:
    """Run a deterministic file-coherent group in one fresh sandbox."""
    sources = [p if p.startswith("/work/src/") else "/work/src/" + p for p in paths]
    group_key = hashlib.sha256("\n".join(sources).encode()).hexdigest()[:12]
    slug = f"group-{group_key}-{RUN_ID}"
    junit = f"/results/{slug}.junit.xml"
    targets = sources
    argv = ["-m", "pytest", "--basetemp=/tmp/pytest-tmp", "-o", "cache_dir=/tmp/pytest-cache", f"--junitxml={junit}", *targets]
    guard = """import json, os, re
ALLOWED = set(SAFE_ENV_NAMES) | {"PYTEST_VERSION", "PYTEST_CURRENT_TEST"}
PHASE_PATH = None
CRED = re.compile(r"(KEY|TOKEN|PASS|SECRET|CREDENTIAL)", re.I)
def _check():
 bad=sorted(k for k in os.environ if CRED.search(k)); extra=sorted(set(os.environ)-ALLOWED)
 assert not bad and not extra, {"credential_names":bad,"extra_names":extra}
def pytest_sessionstart(session):
 global PHASE_PATH
 _check(); p=getattr(session.config.option,"xmlpath",None)
 PHASE_PATH=p.replace(".junit.xml",".phases.jsonl") if p else None
 if PHASE_PATH: open(PHASE_PATH,"w").close()
def pytest_collection_finish(session): _check()
def pytest_runtest_logreport(report):
 if PHASE_PATH:
  with open(PHASE_PATH,"a") as f: f.write(json.dumps({"nodeid":report.nodeid,"phase":report.when,"outcome":report.outcome,"duration":report.duration})+"\\n")
""".replace("SAFE_ENV_NAMES", repr(sorted(SAFE_ENV)))
    temp = RESULTS / f"safe_env_guard.{os.getpid()}.{threading.get_ident()}.tmp"
    temp.write_text(guard)
    temp.replace(RESULTS / "safe_env_guard.py")
    bootstrap = (
        "import gateway.run\nimport os, runpy, sys\n"
        f"allowed = {SAFE_ENV!r}\n"
        "os.environ.clear(); os.environ.update(allowed)\n"
        f"sys.argv = ['pytest', *{argv[2:]!r}]\n"
        "runpy.run_module('pytest', run_name='__main__')\n"
    )
    started = time.time()
    try:
        run = invoke(["-c", bootstrap], timeout=timeout)
        code, output = run.returncode, run.stdout
    except subprocess.TimeoutExpired as e:
        code = 124
        partial = e.stdout or ""
        if isinstance(partial, bytes): partial = partial.decode("utf-8", errors="replace")
        output = partial + "\nRUNNER_TIMEOUT\n"
    log_path = RESULTS / f"{slug}.log"
    log_path.write_text(output + f"\nEXIT_CODE={code}\n")
    junit_host = RESULTS / f"{slug}.junit.xml"
    cases=[]
    if junit_host.exists():
        try:
            for case in ET.parse(junit_host).iter("testcase"):
                phases=[x.tag for x in list(case) if x.tag in {"failure","error","skipped"}]
                cases.append({"name":case.attrib.get("name",""),"classname":case.attrib.get("classname",""),"phase_outcomes":phases or ["passed"],"duration":float(case.attrib.get("time","0"))})
        except (ET.ParseError,OSError): pass
    phase_host=RESULTS / f"{slug}.phases.jsonl"
    events=[]
    if phase_host.exists():
        for line in phase_host.read_text().splitlines():
            if line.strip(): events.append(json.loads(line))
    file_receipts=[]
    for path in paths:
        prefix=path+"::"
        # Keep exact canonical node IDs from pytest's logreport stream; JUnit
        # case names alone are not unique across the suite.
        file_events=[e for e in events if e["nodeid"].startswith(prefix)]
        node_ids=sorted({e["nodeid"] for e in file_events})
        file_cases=[{"nodeid":node,"phase_outcomes":[e["outcome"] for e in file_events if e["nodeid"]==node],"duration_seconds":sum(e["duration"] for e in file_events if e["nodeid"]==node)} for node in node_ids]
        item={"file":path,"group_junit":str(junit_host),"group_log":str(log_path),"phase_events":file_events,"testcases":file_cases,"source_sha256":GATEWAY_SHA,"runner_sha256":RUNNER_SHA,"candidate_git_head":subprocess.run(["git","-C",str(ROOT),"rev-parse","HEAD"],capture_output=True,text=True,check=True).stdout.strip(),"run_id":RUN_ID,"exit_code":code}
        file_path=RESULTS/f"{slug}-{hashlib.sha256(path.encode()).hexdigest()[:10]}.json"
        record(file_path.name,item); file_receipts.append(str(file_path))
    result={"files":paths,"argv":argv,"exit_code":code,"elapsed_seconds":round(time.time()-started,3),"source_sha256":GATEWAY_SHA,"runner_sha256":RUNNER_SHA,"candidate_git_head":subprocess.run(["git","-C",str(ROOT),"rev-parse","HEAD"],capture_output=True,text=True,check=True).stdout.strip(),"tracked_diff_sha256":tracked_diff_sha256(),"run_id":RUN_ID,"junit":str(junit_host),"log":str(log_path),"phase_events_file":str(phase_host),"phase_events":events,"testcases":cases,"per_file_receipts":file_receipts,"output_tail":output[-3000:]}
    record(f"{slug}.json",result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["preflight", "pytest", "shard", "collect"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if args.mode == "preflight":
        result = preflight()
        print(json.dumps(result, indent=2))
        return 0 if result.get("preflight_ok") else 1
    receipt = RESULTS / "PREFLIGHT.json"
    if not receipt.exists() or not json.loads(receipt.read_text()).get("preflight_ok"):
        print("REFUSING: successful preflight receipt required")
        return 90
    preflight_receipt = json.loads(receipt.read_text())
    current_head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if preflight_receipt.get("candidate_git_head") != current_head or preflight_receipt.get("runner_sha256") != RUNNER_SHA or preflight_receipt.get("candidate_gateway_run_sha256") != GATEWAY_SHA or preflight_receipt.get("tracked_diff_sha256") != tracked_diff_sha256():
        print("REFUSING: stale preflight candidate/runner/gateway metadata; rerun preflight")
        return 91
    if args.mode == "collect":
        result_path = RESULTS / f"DEFAULT-COLLECTION-{RUN_ID}.json"
        code = r'''import json, os, pathlib, pytest
os.environ['PYTEST_PLUGINS']=''
class Recorder:
 def pytest_collection_finish(self, session):
  items=[]
  for item in session.items:
   items.append({'nodeid':item.nodeid,'path':str(item.path.relative_to('/work/src')) if str(item.path).startswith('/work/src/') else str(item.path),'markers':sorted(m.name for m in item.iter_markers())})
  pathlib.Path('/results/DEFAULT-COLLECTION-RUN_ID.json').write_text(json.dumps({'nodeids':items},indent=2))
rc=pytest.main(['/work/src/tests','--collect-only','-q','--basetemp=/tmp/collect-tmp','-o','cache_dir=/tmp/pytest-cache'],plugins=[Recorder()])
raise SystemExit(rc)'''.replace('RUN_ID', RUN_ID)
        run = invoke(["-c", code], timeout=args.timeout)
        receipt = {"exit_code": run.returncode, "runner_sha256": RUNNER_SHA, "candidate_git_head": current_head, "candidate_gateway_run_sha256": GATEWAY_SHA, "tracked_diff_sha256": tracked_diff_sha256(), "run_id": RUN_ID, "collection_receipt": str(result_path), "output_tail": run.stdout[-3000:]}
        record(f"COLLECTION-INVOCATION-{RUN_ID}.json", receipt)
        print(json.dumps(receipt, indent=2))
        return run.returncode if result_path.exists() else 1
    if args.mode == "pytest":
        forwarded = args.args
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        # A single invocation is a single bounded worker.
        result = run_file(" ".join(forwarded), args.timeout) if len(forwarded) == 1 else None
        if result is None:
            print("Use `pytest <one-file-or-nodeid>`; shard mode accepts multiple whole files.")
            return 2
        print(result["output_tail"] + f"\nEXIT_CODE={result['exit_code']}")
        return result["exit_code"]
    if not 1 <= args.workers <= 2:
        print("REFUSING: workers must be 1 or 2")
        return 2
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        print("REFUSING: invalid shard index/count")
        return 2
    files = sorted(set(args.args))
    if not files or any("::" in p for p in files):
        print("REFUSING: shard mode accepts whole-file paths only")
        return 2
    selected = [p for i, p in enumerate(files) if i % args.shard_count == args.shard_index]
    group_size = 24
    groups = [selected[i:i+group_size] for i in range(0,len(selected),group_size)]
    manifest = {"source_sha256": GATEWAY_SHA, "runner_sha256": RUNNER_SHA, "candidate_git_head": current_head, "tracked_diff_sha256": tracked_diff_sha256(), "run_id": RUN_ID, "files_input_sorted": files, "shard_index": args.shard_index, "shard_count": args.shard_count, "selected_files": selected, "selected_group_count": len(groups), "group_size_files": group_size, "groups": groups, "workers_requested": args.workers, "worker_cap": 2, "resource_policy": "nice +10, groups of sorted complete files, per-group timeout, each pytest group in a fresh private user/net/PID/IPC namespace", "default_selection": "pytest configuration and marker policy from candidate remain active"}
    manifest_name = f"SHARD-MANIFEST-{RUN_ID}.json"
    record(manifest_name, manifest)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(run_group, group, args.timeout) for group in groups]
        for future in futures:
            results.append(future.result())
    outcomes = {"source_sha256": GATEWAY_SHA, "runner_sha256": RUNNER_SHA, "candidate_git_head": current_head, "tracked_diff_sha256": tracked_diff_sha256(), "run_id": RUN_ID, "manifest": manifest_name, "selected_file_count": len(selected), "completed_group_count": len(results), "selected_group_count": len(groups), "completed_file_count": sum(len(r["files"]) for r in results), "exit_codes": [r["exit_code"] for r in results], "testcase_count": sum(len({e["nodeid"] for e in r["phase_events"] if e["phase"] == "call"}) for r in results), "results": results, "complete": len(results) == len(groups)}
    record(f"SHARD-OUTCOMES-{RUN_ID}.json", outcomes)
    print(json.dumps(outcomes, indent=2))
    return 0 if outcomes["complete"] and all(c == 0 for c in outcomes["exit_codes"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
