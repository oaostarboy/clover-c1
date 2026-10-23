"""Read-only diagnostic observer; never edits product state or test witnesses."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
import os

home = Path.home() / ".clover"
out = Path(os.environ["RUNNER_TEMP"]) / "evidence" / "delivery-observations.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
last = None
end = time.monotonic() + 3300
while time.monotonic() < end:
    observation = {}
    try:
        state = json.loads((home / "gateway_state.json").read_text())
        observation["gateway"] = {key: state.get(key) for key in (
            "pid", "code_sha", "gateway_state", "active_agents", "restart_requested")}
    except (OSError, ValueError):
        pass
    try:
        dbpath = home / "state.db"
        with closing(sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            observation["obligations"] = db.execute(
                "SELECT obligation_id,session_key,content,state,attempts,owner_pid,last_error "
                "FROM delivery_obligations ORDER BY created_at"
            ).fetchall()
    except sqlite3.Error as exc:
        observation["ledger_read_error"] = type(exc).__name__
    encoded = json.dumps(observation, sort_keys=True)
    if encoded != last:
        with out.open("a") as stream:
            stream.write(json.dumps(dict(at=time.time(), **observation)) + "\n")
        last = encoded
    time.sleep(.05)
