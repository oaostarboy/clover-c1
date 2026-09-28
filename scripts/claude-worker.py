#!/usr/bin/env python3
"""Run a Claude Code worker and resume it when it runs out of turns.

Usage (same args you'd give `claude -p`, prompt first):
    claude-worker.py "<prompt>" --model claude-sonnet-5 --max-turns 120 [other claude flags]

Streams claude's stream-json to stdout unchanged (so the parent's
claude-stream-json parser keeps working). If the run ends with
subtype=error_max_turns, it re-runs `claude -p --resume <session_id>` with a
"keep going" message, up to CLAUDE_WORKER_RESUMES times (default 3). If the
final answer admits the job isn't finished, it nudges once more.
Exit code: claude's last exit code.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

RESUMES = int(os.environ.get("CLAUDE_WORKER_RESUMES", "3"))
CONTINUE_MSG = (
    "You ran out of turns before finishing, and your turn budget has been reset. "
    "Continue the ORIGINAL task from where you stopped. Don't redo finished work. "
    "Commit as you go. When everything is done, give the final answer in the "
    "format the task asked for."
)
NUDGE_MSG = (
    "Your answer says the task isn't finished. Keep going and finish the remaining "
    "items now. Only stop if something is truly blocked; then name exactly what and why. "
    "End with the final answer in the format the task asked for."
)
UNFINISHED = re.compile(
    r"(?i)\b(?:didn'?t|did not|haven'?t|have not|couldn'?t|could not)\s+(?:yet\s+)?"
    r"(?:finish|complete|get to|verify)\b|\bnot (?:fully )?(?:verified|complete)\b"
    r"|\bremains? (?:incomplete|unfinished)\b"
)


def run(argv: list[str]) -> tuple[int, dict]:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, bufsize=1)
    last: dict = {}
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            last = obj
    return proc.wait(), last


def strip_prompt_flags(flags: list[str]) -> list[str]:
    out, skip = [], False
    for f in flags:
        if skip:
            skip = False
            continue
        if f in ("--resume", "-r", "--session-id"):
            skip = True
            continue
        out.append(f)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    prompt, flags = sys.argv[1], strip_prompt_flags(sys.argv[2:])
    base = ["claude", "-p"]
    if "--output-format" not in flags:
        flags += ["--output-format", "stream-json", "--verbose"]
    code, res = run(base + [prompt] + flags)
    nudged = False
    for _ in range(RESUMES):
        sid = res.get("session_id")
        if not sid:
            break
        if res.get("subtype") == "error_max_turns":
            msg = CONTINUE_MSG
        elif not nudged and not res.get("is_error") and UNFINISHED.search(str(res.get("result") or "")):
            msg, nudged = NUDGE_MSG, True
        else:
            break
        print(f"[claude-worker] resuming {sid} ({'out of turns' if msg is CONTINUE_MSG else 'not finished'})",
              file=sys.stderr, flush=True)
        code, res = run(base + [msg, "--resume", sid] + flags)
    return code


if __name__ == "__main__":
    sys.exit(main())
