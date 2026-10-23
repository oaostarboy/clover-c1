# Updating Clover

Run `/update` in chat, or `clover update` in a terminal. Both work the same way
on every version, including Clover 1.5.6 and older. You do not need to stop the
gateway or schedule a maintenance window first.

## Updating from Clover 1.5.6 or older

A gateway started before 1.5.7 has the old restart code loaded, and an update
cannot change code that is already running. The update therefore handles that
one gateway with extra care:

1. The update downloads the new files and installs the new version.
2. Before the old gateway is restarted, the updater waits for a quiet moment:
   no agent is working and no reply of that gateway is still being sent. It
   waits up to 90 seconds, then restarts anyway, so an update is never stuck.
3. The gateway restarts on the new version. From then on it uses the normal
   delivery-aware restart and none of this applies again.

What you can see the first time, only if a reply was being sent at the exact
moment of the restart:

- **One reply may arrive twice.** The second copy starts with
  "Recovered reply ... may be a duplicate". Only ordinary reply text is ever
  sent again. Tools, scheduled jobs, payments and messages to other people are
  not run a second time.
- Slash-command answers (for example the `/update` result itself) are not
  tracked by the old gateway, so they cannot be recovered if the restart lands
  exactly while they are being sent.
- Your saved sessions and data are not changed or replaced by the update. The
  database format is the same in 1.5.6 and later, so rolling back the code
  still reads it.

If the update cannot finish (for example, it is offline and cannot repair an
old SQLite library while a database is in WAL mode), it says so plainly: the
new files were downloaded, the running version was **not** restarted, and
`clover update` can be run again to finish. It never reports "nothing was
changed" after files were downloaded.

When the Python runtime's SQLite is too old and cannot be replaced right now
(no network, no `uv`), but every Clover database is already in the safe
rollback-journal mode, the update continues with a warning instead of failing.
Clover keeps its databases out of WAL mode on that runtime, and the next
`clover update` retries the repair.

## Windows: the first update from 1.5.6

On Windows the 1.5.6 updater pauses and may stop the gateway **before** it
downloads the new files, so none of the steps above can protect that single
update. Expect the gateway to be down for a short time while the update runs,
and any chat message being answered at that moment may be interrupted. Your
data is kept, and the gateway comes back on the new version at the end. Every
later update uses the safe path.

If the gateway is not running after a Windows update, run
`clover gateway start`.

## Rolling back

The update takes a consistent snapshot of your state before changing anything,
and a watcher restores the previous code and configuration if the new gateway
does not become healthy within about 90 seconds. The database is never rolled
back automatically; the format has not changed, so the older version reads the
latest data. Do not overwrite a newer database with an older snapshot just to
undo a code update.
