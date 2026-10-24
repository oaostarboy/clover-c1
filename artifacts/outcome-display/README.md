# Outcome renderer patch for review

Baseline: `3b54f413376eb99f8b83bfd4a0a340d01825014d` (published 1.5.9).
This stays 1.5.9 maintenance. No version bump, tag change, updater product change, live install, sibling modification, restart, push or Telegram delivery occurred.

## Changes
- One display-only classifier reads lifecycle status and existing structured task outcome. Real execution failure wins over a positive summary. Interrupted/cancelled remain stopped.
- Unified and classic cards show partial, blocked, unverified and unfinished separately; mixed batches keep each count. Only verified task completion (or the explicit legacy verdict) is success. No arbitrary summary prose becomes evidence.
- Default terminal marks: 🍀 success, 🥀 real failure, ☘️ incomplete/unverified, ⏹ stopped. Working/waiting/tool marks remain unchanged. Skin message packs can set `done_mark`, `fail_mark`, `incomplete_mark`, `stopped_mark`; spinner marks remain fallback.
- Read-only Octavia metadata check found selected `tentacle`, messages done `🫧`, fail `🦑`. These explicit custom marks are respected. Palette, branding, voice and live config were not changed.
- Late/replayed updates remain idempotent. Finished stopped work is counted as stopped rather than silently turning a mixed batch green; stopped rows still leave the running card.

## Evidence
- Original baseline tests: 458 passed.
- Exact final test set on baseline source: 75 expected assertion failures, 437 passed, zero errors.
- Candidate, same final test set/interpreter: 512 passed, zero failures/errors.
- `verification.json` records exact failure node IDs and tested source hashes; `test-command.json` gives the test set/interpreter. XML and raw receipts are alongside them.
- `PREVIEW.md` and `render-previews.json` contain actual unit-render output for all three photographed batch shapes and six terminal outcomes. Real Telegram formatting and EDIT(finalize=True) ran; only the network bot was mocked. The `**>` expandable quote opener is valid MarkdownV2 syntax, not an unformatted literal bold marker.
- Source diff reviewed locally; added-line security scans found no concerns. Independent review belongs to the parent and is not claimed here. No full-suite/native CI or live phone verification is claimed.

## Environment
Tests used an isolated `.venv` in this worktree (Python 3.12), reading existing runtime-158 dependencies through a local .pth file. No packages were installed into the shared runtime. Test homes/temp files and the exact baseline source archive stayed under this worktree. Those large local directories are ignored, not part of the patch.

## Review notes
Existing successful test fixtures now supply their explicit success verdict; exit alone must not imply success. Tests formerly expecting not-done/capped workers to be failed now assert unfinished. Stopped, failed, and success marks follow the active skin. Lifecycle/task-outcome parsing/delivery, gateway/run.py, tool behavior, one-card/follow behavior, models/stats, and Stop controls are not changed.
