# Suni root cause — read-only observation, 2026-10-24 01:08–01:10Z

Actual launchd `gui/501/ai.clover.gateway` owns **40563** (stderr_timestamp wrapper). Its sole child **40564** owns gateway.pid and gateway_state.json and listens on 127.0.0.1:8645. Both have run since Oct 17 15:54:14 local; neither is a dead/stale PID at observation. Status timestamp Oct 21 is stale, so its active_agents=0 is NOT idle evidence.

Published 1.5.9 `3b54f413376eb99f8b83bfd4a0a340d01825014d`, `_proved_stderr_wrapper`, rejects the real process at TWO lexical-identity seams:

1. Wrapper argv[0] = Homebrew `.../Resources/Python.app/Contents/MacOS/Python`; inner command[0] = `/Users/ant/agents/clover-c1/venv/bin/python`. Therefore `command[0] != argv[0]` returns False before inspecting the admitted worker.
2. Worker argv[0] is also the Homebrew framework executable, not inner command[0]. Thus `child_command != command` and the unchanged `_prepare_child_command(command, wrapper.environ())` reject it again if only seam 1 is removed. `--external-supervisor` is already present.

This is native framework Python's observable argv shape, not proof of a foreign install. Plist ProgramArguments identify the intended venv on BOTH sides; venv/python resolves to Homebrew framework bin/python3.12. Worker mapped native libraries include THIS venv; editable direct_url points to `/Users/ant/agents/clover-c1` (metadata still 1.5.6). Do NOT accept any base-Python alias without independent install/owner validation: different venvs can share it.

Published full discovery maps the worker via strict profile PID file, but includes wrapper through service/argv discovery. Published `_gateway_pid_install_verdict` returns `unknown` on macOS. The legacy child enters `admitted`, then the wrapper mismatch yields the exact receipt reason `PID 40563: no owner-bound gateway status`.

No live mutation/restart, environment/config/auth dumps, DB reads, product imports, installed probes, or dependency changes were performed. Local fixture files only. Parent owns live recovery. Current branch base is 0c8f3cd (earlier fail-closed policy), NOT published 1.5.9; replay must pin published source explicitly.

Evidence: live-selected.json (process/plist/listener), live-metadata.json (PID/status/receipt/pending marker/venv identity). Pending marker expected_sha remains 3b54f413; current receipt failed 01:03:05–01:03:15Z, empty gateway_restart, watcher disarmed true. Preserve both and pre-update snapshot.
