<!--
Release notes shown to users in the chat message they get after /update.

HOW TO EDIT (by hand, at release time)
  * One section per version, newest first:
        ## <version> | <release name> | <YYYY-MM-DD>
  * Then 3-6 short, plain-language bullets starting with "- ". Write for a
    non-technical user: what they can now do or what stopped breaking.
    No file names, no jargon, no PR numbers. Keep each bullet under ~110 chars.
  * A section containing the line  <!-- draft -->  is a placeholder: it is
    never shown to users (only its name/version are). Remove the line when you
    fill in the bullets. tests/clover_cli/test_release_notes.py fails if the
    entry for the current __version__ is still a draft, so a version bump
    cannot ship with a placeholder.
  * Bump order at release: fill this file, then __version__, pyproject.toml, uv.lock.
-->

## 1.3.0 | Clover C1.3 | 2026-10-10
- Telegram doesn't answer the same message twice after a reconnect.
- A busy database or a website firewall no longer cuts off replies or blames your API key.
- Stopping a background task can no longer take Clover down with it.
- Telegram restarts its connection by itself if it stops picking up messages.
- Helper cards show what the helper is actually doing again, and helpers start from any folder.
- /update no longer reports a false failure, and the main chat's work limit is now off.

## 1.2.0 | Clover C1.2 | 2026-10-10
- Long-running tool calls now stop cleanly, so you can send a new message right away.
- Background helpers show clearer progress and only report success when their work is verified.
- Code workspaces start more reliably, while isolated tasks stay separate from your project.
- Clover reuses warm code sessions, cleans up idle ones, and keeps reset tasks tracked.
- Old "working..." previews and stale replies are cleaned up, so you don't see leftover or doubled messages.
- Fixed: the Docker image builds again.

## 1.1.4 | Clover C1.1.4 | 2026-10-08
- Before an update starts, Clover now checks your setup and tells you in plain words if something would stop it, and changes nothing.
- New: send /update check to see if you're ready to update, without updating.
- Behind the scenes: new versions can now be released with one button.

## 1.1.3 | Clover C1.1.3 | 2026-10-08
- Fixed: a helper's progress box could freeze or disappear after a short Telegram connection drop.
- The box now moves to the new connection, or posts itself again, instead of getting stuck.

## 1.1.2 | Clover C1.1.2 | 2026-10-07
- Fixed: the what's-new message could go missing after /update when you had local changes.
- Fixed: on some Linux setups (like a Raspberry Pi) /update could finish without telling you.
- If the update result file is ever garbled, you now still get the finished message instead of silence.

## 1.1.1 | Clover C1.1.1 | 2026-10-07
- Long jobs now move to a background helper, so your agent stays free to chat while it works.
- Helper cards show what the helper is doing and what it's aiming for.
- After /update, you now see the version you're on and what's new.
- Fixed: /update on Windows failing when Clover is started by a Scheduled Task.
- Fixed: a false warning saying a file wasn't changed when it was.

## 1.1.0 | Clover C1.1 | 2026-10-07
- This is the first named release: Clover C1.1.
- /update on Windows now works with the bot running, including Task Scheduler setups.
- Updates on Linux and macOS now restart the bot on the new version and tell you when it is back.
