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

## 1.1.1 | Clover C1.1.1 | TBD
<!-- draft -->
- TODO(release): replace these placeholder lines with 3-6 user-facing bullets.

## 1.1.0 | Clover C1.1 | 2026-10-07
- This is the first named release: Clover C1.1.
- /update on Windows now works with the bot running, including Task Scheduler setups.
- Updates on Linux and macOS now restart the bot on the new version and tell you when it is back.
