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

## 2.0.2.2 | Clover C2.0.2.2 | 2026-10-30
- Desktop text input no longer overlaps assistant responses when streaming or completing answers.
- Desktop background smoothly transitions into an elegant frosted-glass look on your first message.
- Subagents now default to low reasoning effort for faster execution while the main agent plans.
- Retired models were cleaned from provider catalogs to keep model pickers accurate and current.
- Scheduled cron notifications are delivered in clean quote cards with job identifiers and hints.
- Improved Linux desktop application launch and sandbox compatibility across desktop environments.

## 2.0.2.1 | Clover C2.0.2.1 | 2026-10-29
- Updates from earlier releases now create and install the Clover desktop application reliably.
- Updating while already on the latest code automatically builds and installs the desktop app.
- Gateway startups automatically provision the desktop app in the background if it is missing.
- Windows updates recognize standard Python launcher processes without stopping mid-install.
- Automatic update rollback on Windows now arms and recovers cleanly after a restart.

## 2.0.2 | Clover C2.0.2 | 2026-10-29
- Automatically detects if a configuration already exists on startup, bringing new users straight to onboarding.
- Personalize your Clover agent during first-run onboarding by naming your AI companion directly.
- Setup wizard and desktop onboarding seamlessly persist custom agent branding across all chat surfaces.

## 2.0.1 | Clover C2.0.1 | 2026-10-29
- Automatically builds the desktop app during update on macOS, Windows, and Linux desktops.
- Automatically installs Clover to the macOS Applications folder and Windows Start Menu / Desktop.
- Chat and CLI users can update anytime to receive the desktop app with zero extra setup.

## 2.0.0 | Clover C2.0 | 2026-10-29
- Interactive first-run desktop tour guides you through chat, preview, terminal, and shortcuts.
- Apple Liquid Glass onboarding with live ASCII clovers and native blur across macOS, Windows, and Linux.
- Smooth conversation continuation and calm particle animations dissolve gracefully into chat.
- Full accessibility support for reduced motion, high contrast mode, and transparent window fallbacks.

## 1.5.9.1 | Clover C1.5.9.1 | 2026-10-25
- Long handoff replies and drafts stay complete, including spacing between message chunks.
- Concurrent requests cannot launch the same helper twice.
- Helpers stop safely before starting work if required history storage cannot open.
- Valid structured helper results no longer trigger a retry that loses their completion status.
- Database saves wait within their existing budget when short reads temporarily block a commit.

## 1.5.9 | Clover C1.5.9 | 2026-10-23
- Helpers now start when your chat is not inside a code project, instead of being refused after a restart.
- Helpers working in a real code project still get their own safe copy, and still stop if that copy cannot be made.
- When a helper is refused, the message now names the exact setting to change.
- The two helper isolation settings are now known, so changing them no longer shows a warning.
- Identified 1.5.6 gateways no longer stop an update just because their reply-delivery code is older.
- Updates keep restart work pending if ownership or a quiet restart cannot be verified; one reply may repeat.

## 1.5.8 | Clover C1.5.8 | 2026-10-23
- Saving no longer gets stuck behind a leftover database read, so busy-storage failures should be much rarer.
- A turn whose final save failed is no longer shown as completed; your real output is kept.
- Save-failure messages no longer say to resend after a tool may have already run.
- Pressing Stop no longer hides a save failure in the terminal, gateway or desktop app.

## 1.5.7 | Clover C1.5.7 | 2026-10-23
- Updates wait for complete reply delivery before restarting supported gateways.
- Older gateways may need one terminal maintenance update before automatic updates work again.
- Managed Python repair checks SQLite safety and preserves the latest saved work.
- macOS updates recognize verified service wrappers without trusting unknown processes.
- Windows updates verify their own process chain and recognize managed Python path aliases.
- Busy storage and interrupted jobs now report uncertainty instead of claiming work did not run.

## 1.5.6 | Clover C1.5.6 | 2026-10-16
- Optional work tracking keeps unfinished jobs, corrections and relevant context across chats; off by default.
- Helper output stays unverified until required checks pass; release and delivery remain separate steps.
- Direct answers stay in your reply when a helper takes over the rest of the job.
- Successful council route checks no longer trigger a second answer.
- Windows installs keep dependency files unchanged, and running-gateway updates retain their result.
- Safer Windows work-record reads and clearer native checks improve cross-platform verification.

## 1.5.5 | Clover C1.5.5 | 2026-10-13
- If an agent deletes the folder it's working in, its terminal and helpers keep working instead of getting stuck.
- Helpers that start automatically now get their own clear name, the full instructions they need, and the default model.

## 1.5.4 | Clover C1.5.4 | 2026-10-12
- Every subagent job now gets a clean card, and the card is back after a restart.
- Joined cards say "3 subagents", and a finished card sits right under the reply.
- Subagent titles name the real work, and each subagent can collapse on its own.
- A job that runs long is handed to a subagent with full context instead of just stopping.
- Subagents work in their own sandbox, read anywhere, and can no longer touch a live install.
- Finished subagent cards now land under the reply, even when the results arrive while the agent is busy.

## 1.5.3 | Clover C1.5.3 | 2026-10-12
- Agents hand big jobs to subagents more reliably, and quick questions stay quick.
- If a job turns out bigger than expected, it's handed to a subagent automatically.
- The subagent card names the job, like "comparing 3 options for adding a second GPU".
- Finished jobs shrink to one 🍀 line, and the bottom line shows what's running and what's done.
- Joined cards show a short title like "3 jobs" instead of cut-off names.
- Small talk like "thanks" no longer spends tokens on routing.

## 1.5.2 | Clover C1.5.2 | 2026-10-11
- Subagent work now shows in one tidy card that stays at the bottom of your chat; tap it to watch.
- Each subagent gets its own checklist, with its live steps, model, tool calls and time.
- A new job joins the card that's already running instead of starting a second one.
- Subagents no longer fail when Clover's database is busy for a moment.
- Chat now says "subagents", and task text is no longer pasted into the chat.
- Outside workers (like other AI models) now show in the same card as your subagents.

## 1.5.1 | Clover C1.5.1 | 2026-10-11
- When a helper takes over, you get a short line and a checklist that ticks itself off as it works.
- Done steps get a 🍀, and a 🍂 shows when a helper couldn't finish the job.
- Clo, the clover sprite, is now the default look; the old plain look is called "boring".
- Saving a quick note no longer starts an extra helper or sends a second message.
- Stopped helpers say "stopped" instead of "not done", and quiet replies stay quiet.

## 1.5.0 | Clover C1.5 | 2026-10-11
- Each request is sized up for you: quick questions get a direct answer, big jobs go to helpers.
- Independent parts run at the same time; steps that need earlier results run one after another.
- You can keep chatting right after a hand-off, and Clover tells you the helpers are working.
- No final verdict while helpers are still running or have failed; what isn't done is named.
- Stop always wins, and a personality file that names security tools no longer gets blocked.
- Helpers report back together, asking for one works in any chat, and /update shows what's new.

## 1.3.0 | Clover C1.3 | 2026-10-10
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
