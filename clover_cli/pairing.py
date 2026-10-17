"""
CLI commands for the DM pairing system.

Usage:
    clover pairing list              # Show all pending + approved users
    clover pairing approve <platform> <request-id|code>  # Approve a pairing request
    clover pairing revoke <platform> <user_id> # Revoke user access
    clover pairing clear-pending     # Clear all expired/pending codes
"""

def pairing_command(args):
    """Handle clover pairing subcommands."""
    from gateway.pairing import PairingStore

    action = getattr(args, "pairing_action", None)
    if action in {"owner-setup", "owner-confirm", "owner-revoke"}:
        if getattr(args, "platform", None) != "telegram":
            print("Owner verification currently supports Telegram only.")
            return
        telegram_owner_command(action)
        return
    store = PairingStore()

    if action == "list":
        _cmd_list(store)
    elif action == "approve":
        _cmd_approve(store, args.platform, args.code)
    elif action == "revoke":
        _cmd_revoke(store, args.platform, args.user_id)
    elif action == "clear-pending":
        _cmd_clear_pending(store)
    else:
        print("Usage: clover pairing {list|approve|revoke|clear-pending}")
        print("Run 'clover pairing --help' for details.")


def _cmd_list(store):
    """List all pending and approved users."""
    pending = store.list_pending()
    approved = store.list_approved()

    if not pending and not approved:
        print("No pairing data found. No one has tried to pair yet~")
        return

    if pending:
        print(f"\n  Pending Pairing Requests ({len(pending)}):")
        print(f"  {'Platform':<12} {'Request ID':<18} {'User ID':<20} {'Name':<20} {'Age'}")
        print(f"  {'--------':<12} {'----------':<18} {'-------':<20} {'----':<20} {'---'}")
        for p in pending:
            print(
                f"  {p['platform']:<12} {(p.get('request_id') or '-'):<18} {p['user_id']:<20} "
                f"{(p.get('user_name') or ''):<20} {p['age_minutes']}m ago"
            )
        print("\n  Approve with: clover pairing approve <platform> <request-id>")
        print("  The code the bot DM'd the user also works if they relay it.")
    else:
        print("\n  No pending pairing requests.")

    if approved:
        print(f"\n  Approved Users ({len(approved)}):")
        print(f"  {'Platform':<12} {'User ID':<20} {'Name':<20}")
        print(f"  {'--------':<12} {'-------':<20} {'----':<20}")
        for a in approved:
            print(f"  {a['platform']:<12} {a['user_id']:<20} {(a.get('user_name') or ''):<20}")
    else:
        print("\n  No approved users.")

    print()


def _cmd_approve(store, platform: str, code: str):
    """Approve a pairing request id (from ``pairing list``) or a DM'd code."""
    platform = platform.lower().strip()
    code = code.strip()

    if store.looks_like_request_id(code):
        result = store.approve_request(platform, code)
    else:
        result = store.approve_code(platform, code.upper())
    if result:
        uid = result["user_id"]
        name = result.get("user_name") or ""
        display = f"{name} ({uid})" if name else uid
        print(f"\n  Approved! User {display} on {platform} can now use the bot~")
        print("  They'll be recognized automatically on their next message.\n")
    elif store._is_locked_out(platform):
        # Disambiguate: approve_code returns None for both invalid codes
        # and lockout. Tell the operator it's lockout so they don't chase
        # a "wrong code" rabbit hole (#10195).
        import time as _time
        limits = store._load_json(store._rate_limit_path())
        lockout_until = limits.get(f"_lockout:{platform}", 0)
        remaining = max(0, int(lockout_until - _time.time()))
        mins = remaining // 60
        print(
            f"\n  Platform '{platform}' is locked out after too many failed "
            f"approval attempts."
        )
        print(f"  Lockout clears in ~{mins} minute(s).")
        print(
            "  To reset sooner, delete the '_lockout:{0}' entry from "
            "~/.clover/platforms/pairing/_rate_limits.json\n".format(platform)
        )
    else:
        print(f"\n  Pairing request or code '{code}' not found or expired for platform '{platform}'.")
        print("  Run 'clover pairing list' to see pending requests.\n")


def _cmd_revoke(store, platform: str, user_id: str):
    """Revoke a user's access."""
    platform = platform.lower().strip()

    if store.revoke(platform, user_id):
        print(f"\n  Revoked access for user {user_id} on {platform}.\n")
    else:
        print(f"\n  User {user_id} not found in approved list for {platform}.\n")


def _cmd_clear_pending(store):
    """Clear all pending pairing codes."""
    count = store.clear_pending()
    if count:
        print(f"\n  Cleared {count} pending pairing request(s).\n")
    else:
        print("\n  No pending requests to clear.\n")


def setup_verified_telegram_owner() -> bool:
    """Offer after normal setup has connected the gateway; never start it here."""
    from clover_cli.setup import prompt_yes_no
    from gateway.config import Platform, load_gateway_config
    cfg = load_gateway_config().platforms.get(Platform.TELEGRAM)
    if not cfg or not cfg.enabled or not cfg.token:
        return False
    from gateway.pairing import VerifiedOwnerStore
    from gateway.slash_access import policy_from_extra
    store = VerifiedOwnerStore(cfg.token)
    if store.owner_user_id():
        print("Telegram owner already verified for this profile and bot. Proactivity unchanged.")
        return True
    if policy_from_extra(cfg.extra, "dm").enabled:
        print("Telegram's explicit DM admin policy already controls /proactive; left unchanged.")
        return True
    if cfg.extra.get("verified_owner_controls", True) is not True:
        print("Telegram owner verification is disabled by profile or managed policy.")
        return False
    if not prompt_yes_no("Verify your Telegram account for owner controls? (Proactivity stays unchanged)", True):
        print("Owner not verified. Later: clover pairing owner-setup telegram")
        return False
    return telegram_owner_command("owner-setup")


def telegram_owner_command(action: str) -> bool:
    """Local profile authority plus an independently delivered Telegram code.

    Codes never enter command-line arguments, config.yaml, ordinary pairing,
    chat history or model tools. Local authority is filesystem/operator trust,
    not a claim that a terminal itself proves a human is present.
    """
    from gateway.config import Platform, load_gateway_config
    from gateway.pairing import VerifiedOwnerStore
    from clover_cli.setup import prompt
    cfg = load_gateway_config().platforms.get(Platform.TELEGRAM)
    if not cfg or not cfg.token:
        print("Owner not verified: configure Telegram in this profile first.")
        return False
    store = VerifiedOwnerStore(cfg.token)
    try:
        if action == "owner-revoke":
            store.revoke()
            print("Verified Telegram owner and pending owner proofs revoked. Explicit admin and chat access unchanged.")
            return True
        if cfg.extra.get("verified_owner_controls", True) is not True:
            print("Owner not verified: disabled by profile or managed policy.")
            return False
        if action == "owner-setup":
            if store.owner_user_id():
                print("Telegram owner already verified for this profile and bot. Proactivity unchanged.")
                return True
            challenge = store.begin()
            print("Verify only your own Telegram account. Keep these codes private.")
            print("The gateway must be connected to this bot. In its private chat, send:")
            print("/start cloverowner_" + challenge)
            print("Then enter the confirmation code returned to YOUR Telegram account here.")
            print("No ownership is granted until local confirmation. Codes expire in 10 minutes.")
        code = prompt("Telegram confirmation code (Enter to finish later)", password=True)
        if not code:
            print("Owner not verified. After connecting the gateway and sending the challenge, run:")
            print("clover pairing owner-confirm telegram (in this same profile, before expiry)")
            return False
        uid = store.confirm(code)
        if uid and store.is_owner(uid):
            print(f"Telegram owner {uid} verified and saved for this profile and bot.")
            print("Reopen /proactive for controls. Proactivity and guest permissions are unchanged.")
            print("Existing chat admission and explicit admin/managed policy still apply.")
            return True
        print("Owner not verified: code invalid, expired or already consumed. Start owner-setup again.")
    except (OSError, ValueError, TypeError, RuntimeError):
        # Exception messages can contain file contents or credentials. Do not
        # echo them (or either code) into console logs.
        print("Owner not verified: could not confirm the scoped save. Check profile storage permissions.")
        print("If the bot credential or profile location changed, use owner-revoke before owner-setup.")
    return False
