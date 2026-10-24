"""Read-only macOS ownership evidence. Run via pinned SSH stdin, stdlib only."""
import datetime
import json
import pathlib
import plistlib
import subprocess


def run(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=12)
    return {"exit_code": p.returncode, "stdout": p.stdout.strip()}


home = pathlib.Path.home()
result: dict = {"observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
result["platform"] = run(["sw_vers", "-productVersion"])
result["processes"] = run(["ps", "-p", "40563,40564", "-o", "pid=,ppid=,uid=,lstart=,etime=,stat=,comm="])
rows = run(["ps", "-axo", "pid=,ppid=,comm="])
result["direct_children"] = [line.strip() for line in rows["stdout"].splitlines() if len(line.split()) >= 2 and line.split()[1] in {"40563", "40564"}]
for pid in (40563, 40564):
    result[f"pid_{pid}_argv"] = run(["ps", "-p", str(pid), "-o", "args="])
    result[f"pid_{pid}_files"] = run(["lsof", "-a", "-p", str(pid), "-d", "cwd,txt", "-Fn"])
    result[f"pid_{pid}_listen"] = run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"])
for name in ("gateway_state.json", "gateway.pid"):
    p = home / ".clover" / name
    if not p.exists():
        result[name] = {"exists": False}
        continue
    raw = p.read_text()
    try:
        data = json.loads(raw)
    except ValueError:
        data = raw.strip()
    if isinstance(data, dict):
        allowed = {"pid", "gateway_pid", "process_start_time", "process_start_ticks", "code_sha", "code_version", "version", "state", "status", "active_agents", "active_jobs", "updated_at", "last_updated", "started_at", "clover_home", "venv", "python_executable", "restart_requested"}
        result[name] = {"mtime": p.stat().st_mtime, "keys": sorted(data), "selected": {k: v for k, v in data.items() if k in allowed}}
    else:
        result[name] = data
result["launch_agents"] = []
for p in sorted((home / "Library/LaunchAgents").glob("*.plist")):
    if not any(s in p.name.lower() for s in ("clover", "suni")):
        continue
    data = plistlib.loads(p.read_bytes())
    item = {"file": str(p), "selected": {k: data[k] for k in ("Label", "Program", "ProgramArguments", "WorkingDirectory", "KeepAlive", "RunAtLoad") if k in data}}
    label = data.get("Label")
    if label:
        output = run(["launchctl", "print", f"gui/{__import__('os').getuid()}/{label}"])
        safe_prefixes = ("state =", "pid =", "program =", "path =", "runs =", "last exit code =", "active count =")
        item["launchctl_selected"] = {"exit_code": output["exit_code"], "lines": [line.strip() for line in output["stdout"].splitlines() if line.strip().startswith(safe_prefixes)]}
    result["launch_agents"].append(item)
print(json.dumps(result, indent=2))
