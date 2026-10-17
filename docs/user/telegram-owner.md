# Telegram owner verification

Owner verification is an optional local identity-binding feature retained for
compatibility. **It is not required for `/proactive`.** Admitted users with
existing scoped command/control authority can open `/proactive` and tap **On**
after updating, even on older profiles with no owner record. There is no terminal
or code-exchange requirement for those controls. Updating does not enable them.

Chat admission is not proof of ownership: a DM, username, home channel, allowlist,
pairing or unrestricted command policy never creates an owner record. Configured
DM/group admin lists and managed restrictions still determine settings rights.
See [Proactive controls](proactive-workstreams.md) for the current permission rules.

## New installations

Telegram setup offers **Verify your Telegram account for owner controls?** after
the normal gateway connection step. Verification does not start another poller,
enable proactivity, change guest commands or grant chat access.

1. Keep your local setup terminal open. Open your own bot's **private** chat.
2. Send the `/start cloverowner_…` challenge shown by setup. Do not share it.
3. Enter the separate confirmation code the bot returns into that same local
   setup terminal. Never send this code to another person.
4. Setup verifies and saves the account that actually sent the Telegram update.
   This optional record does not change existing command/control authority.

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

If `/proactive` says **Controls locked by command policy**, owner verification
will not bypass that policy. `/proactive status` (or `/proactive setup`) explains
the scoped admin setting. Ask the profile operator to review that permission
policy instead of completing a pairing ceremony.

Operators who still want an explicit owner identity record may use the optional
local command `clover pairing owner-setup telegram` (or
`clover -p NAME pairing owner-setup telegram` for a named profile).

## Permission boundaries

- Owner verification does **not** confer general slash-command admin rights,
  chat access or a bypass of configured admin restrictions.
- Existing source admission is required. With no scoped admin gate, admitted
  users already have command/control authority without an owner record.
  With a gate configured, its admins alone can change proactive settings;
  guest permission to list `/proactive` stays read-only. Groups use their
  separate group policy, not DM admin grants.
- `platforms.telegram.verified_owner_controls: false` disables the legacy owner
  fallback only. Managed denials of that fallback take precedence over user
  aliases, but do not deny independent command/control authority. Use scoped
  admin policy or managed proactive settings to restrict those controls.
- The picker checks account, chat, message, thread, profile, adapter credentials,
  expiry, replay and current admission/control authorization at tap time.

## Revocation and account changes

Run locally in the exact profile:

```
clover pairing owner-revoke telegram
```

This cancels both pending owner proofs and the independent owner grant. It takes
effect on the next owner-record operation; no service restart is required for
this record. It does not revoke independent command/control authority, so an
already-open `/proactive` picker remains valid if that authority still applies.
It does not remove admin grants or chat permissions. Remove those separately
through the existing policy if revoking settings access is intended.
Rebinding requires revocation and fresh verification. Bot credential rotation
or moving/copying the profile invalidates the grant; revoke and verify again.

The record lives at `platforms/telegram-owner.json` within the active profile
home. It contains a scope fingerprint and verified numeric account ID, not bot
credentials or plaintext codes. Writes use the existing pairing atomic writer
and portable cross-process lock. Files use restrictive permissions where the
host supports them. Do not copy or hand-edit this record to enroll someone.

## Trust limits

Telegram updates containing recognizable owner-proof markers are consumed
before ordinary chat processing, batching or observed history. This includes
message text, captions, replied-to text/captions, native quotes and edits.
Only a new direct private message can submit the setup challenge; a caption,
quote, callback or edit cannot submit it. Confirmation still happens locally.
Transport log redaction is a separate safeguard, not the chat isolation boundary.

This textual check does not read proof codes from arbitrary image pixels or
audio, or recognize an unlabelled code pasted without its marker. Do not send
screenshots or recordings of setup secrets to the bot. Enter the confirmation
code only in the local terminal. These safeguards do not erase earlier logs or
history from an installation that already exposed a code.

The local profile operator already controls the profile's files and bot
credentials. This ceremony is not a sandbox against code or people with that
same filesystem authority, nor does a terminal by itself prove a human is
present. It establishes an explicit local authorization plus an actual
Telegram-sender binding, separate from untrusted chat claims. Proactivity keeps
its exact previous value, including Off; enrollment creates no jobs, schedules
or model calls. Only a later authorized On action changes that setting.
