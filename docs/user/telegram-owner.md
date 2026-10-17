# Telegram owner verification

A Telegram account needs explicit authority before it can change persistent
profile settings with `/proactive`. Chat access is not ownership. A DM, a
username, a home channel, an allowlist, guest pairing or an unrestricted slash
menu does not prove who owns the profile.

## New installations

Telegram setup offers **Verify your Telegram account for owner controls?** after
the normal gateway connection step. Verification does not start another poller,
enable proactivity, change guest commands or grant chat access.

1. Keep your local setup terminal open. Open your own bot's **private** chat.
2. Send the `/start cloverowner_…` challenge shown by setup. Do not share it.
3. Enter the separate confirmation code the bot returns into that same local
   setup terminal. Never send this code to another person.
4. Setup verifies and saves the account that actually sent the Telegram update.
   Reopen `/proactive` to see the native Enable / Disable buttons.

Nothing grants ownership until step 3. Setup never accepts a typed user ID as
identity proof. Forwarded messages, bots and public groups cannot supply proof.
Codes expire after ten minutes. If the gateway is not connected or you leave
setup, it reports **not verified**, not success. Connect the gateway through its
normal approved setup, send the challenge and finish in the same profile with:

```
clover pairing owner-confirm telegram
```

If the request expired or the bot's reply failed, begin a fresh verification.
The confirmation prompt hides the code; it is not a command-line argument.

## Existing installations

Existing explicit DM administrator grants still work. Setup preserves them
without inferring a new owner or changing the slash gate. A genuinely verified
owner record is reused only for the same profile location and bot credentials.

If `/proactive` says **Controls locked**, open `/proactive setup` (or
`/proactive status`). Ask the person who set up Clover to run this locally:

```
clover pairing owner-setup telegram
```

This uses the same verification steps as new setup. No historical message,
paired guest or sole allowlisted sender is silently upgraded. An installation
with no trusted owner proof cannot securely unlock itself from the phone alone.
The local profile operator must assist. For a named profile, use the normal
profile selector, for example `clover -p NAME pairing owner-setup telegram`.

## Permission boundaries

- The independent grant enables only the private Telegram owner fallback for
  profile proactive controls. It does **not** confer general slash-command admin
  rights. Guest command permissions and all admission lists remain unchanged.
- Existing chat admission is still required to use the controls. Verify chat
  access separately through the normal allowlist/pairing process.
- An enabled explicit DM admin policy is authoritative, even if it excludes the
  verified owner. Group controls still require explicit group admin permission.
- Administrators can disable the fallback with
  `platforms.telegram.verified_owner_controls: false`. A managed denial takes
  precedence over user settings; current managed denials are checked again at
  use. This switch does not revoke independent explicit admin grants.
- The picker still checks the account, chat, message, thread, profile, adapter,
  expiry, replay and current authorization at tap time.

## Revocation and account changes

Run locally in the exact profile:

```
clover pairing owner-revoke telegram
```

This cancels both pending owner proofs and the independent owner grant. It takes
effect on the next operation, including an already-open picker; no service
restart is required for this record. It does not remove explicit admin grants
or guest/chat permissions. Remove those separately if revoking them is intended.
Rebinding requires revocation and fresh verification. Bot credential rotation
or moving/copying the profile invalidates the grant; revoke and verify again.

The record lives at `platforms/telegram-owner.json` within the active profile
home. It contains a scope fingerprint and verified numeric account ID, not bot
credentials or plaintext codes. Writes use the existing pairing atomic writer
and portable cross-process lock. Files use restrictive permissions where the
host supports them. Do not copy or hand-edit this record to enroll someone.

## Trust limits

The local profile operator already controls the profile's files and bot
credentials. This ceremony is not a sandbox against code or people with that
same filesystem authority, nor does a terminal by itself prove a human is
present. It establishes an explicit local authorization plus an actual
Telegram-sender binding, separate from untrusted chat claims. Proactivity keeps
its exact previous value, including Off; enrollment creates no jobs, schedules
or model calls. Only a later authorized Enable action changes that setting.
