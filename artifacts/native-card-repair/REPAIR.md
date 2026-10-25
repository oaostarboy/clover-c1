# Native split-card and follow repair (private 1.5.9 candidate)

## Confirmed causal defect

Published baseline: `3b54f413376eb99f8b83bfd4a0a340d01825014d`.

A valid native handoff with an empty/missing `say` produces a whole-card final reply. `bind_ack_card` previously attached that reply directly, leaving `split_opener=False`. The later running-editor lookup excluded it, and another native handoff received a second live card. Only the newest editor followed ordinary messages, stranding the older active job. This is separate from the already-published empty-constructor registry repair, which is unchanged.

Read-only live field-only query confirmed the recorded Aryeo delegate_task handoff had an empty `say`. This fits the reproduced defect; it does **not** prove Octavia's historical in-memory binding state or establish whether the poster's worker had finished at the time of the later screenshot.

## Small repair

- A sent unified card with no opener becomes joinable. Its dispatch joins the existing live editor for the same adapter/chat/topic; its already-sent duplicate stays until a successful normal, formatted shared-card edit confirms the section is present.
- Successful edits sweep retained duplicate IDs. Failed delete results remain available for a later sweep. Existing edit throttling, server backoff and follow debounce still own scheduling.
- Every gateway join immediately records the shared message ID for all dispatches, rather than waiting for the next repost.
- The new producer regression exposed an existing Telegram-topic collision as well. Native unified follow/grouping and completion-follow keys now include the existing `thread_id` metadata. `BasePlatformAdapter.send` forwards this metadata to its existing outbound observer (one line). Classic and non-Telegram follow keys retain their prior behavior. No general routing framework was added.

Production changes are only `gateway/delegation_activity.py` and the metadata-forwarding line in `gateway/platforms/base.py`. No changes to router, checklist/outcome classification, run.py or turn_context.py.

## Real producer verification

`tests/gateway/test_native_card_grouping.py` exercises real `delegate_task` admission, asynchronous registry, accepted dispatch receipt, queued/start/tool/complete producer callbacks, real TurnRunner callback, router ack generation, BasePlatformAdapter binding and TelegramAdapter send/edit/formatting with fake Bot API I/O. Only child construction/work and Bot transport are fake. It does not inject cards, call join manually or manually repost.

Cases cover:
- Two held-active native jobs, separate turns, empty say, 160 worker-free final replies, then a tool bubble and ordinary response.
- Exactly one persistent live card strictly after the latest outbound response when the actual 1.5-second callback fires; both durable dispatch card IDs match; useful models/checklists/tools remain; late updates edit the current message; each old ID deletes once.
- Concurrent native turn placement; normal opener behavior remains covered by both new and unchanged tests.
- Completion, blocked task report and cancellation: the ended part leaves, the other job follows, and a pending callback never resurrects the final ended child.
- Independent chats, Telegram topics including the main chat, and separate profile adapters. Topic completion does not move/delete a foreign card.
- Real finalize=True Telegram edit formatting, including the intentional MarkdownV2 expandable-quote delimiter rather than confusing that delimiter with leaked markup.

Exact same 12-file union and identical new test file on baseline and candidate:
- Baseline: **442 passed / 2 expected failures**, zero errors/skips.
- Candidate: **444 passed**, zero errors/skips.
- Parsed JUnit confirms **444 unique, identical test identities** in both runs. See `results.json`, `baseline-tmpfs.xml`, `candidate-tmpfs.xml` and matching logs.

The initial completed candidate also passed all 444 checks with `/var/tmp`. Subsequent `/var/tmp` attempts failed on host Btrfs ENOSPC during atomic config/record replacement; those raw logs/XML are retained and are not counted as code regressions or clean baseline evidence. Final comparable runs used an isolated `TMPDIR=/dev/shm`, outside every real Clover home. Atomic editor tool writes also hit ENOSPC; the one-line throttle-preservation correction used a verified in-place edit of this worktree's file only.

## Reusable workflow

For native card bugs, start at accepted delegate_task receipt and keep child progress through `_build_child_progress_callback` plus its mutable production identity reference. A fake child whose callback only writes transcripts does not prove card activity. Keep the child held alive through later turns, wait for the real follow timer, and compare transport IDs, editor membership and every durable dispatch location. Check both a missing opener and a real opener; a model-first header is valid unified v8 layout, not a classic-fallback diagnosis. Treat temp-filesystem/config write failures as environment failures, retain raw receipts and rerun the same union in a safe isolated filesystem.

## Scope limits

Private source repair only. No live configuration/runtime changes, restarts, Telegram sends, push, CI, merge, version bump or tag. The release version remains 1.5.9. The parent owns independent review, combined-tree validation and normal release delivery. These tests certify the exercised offline transport path, not phone acceptance or forensic proof of every screenshot.
