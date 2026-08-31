"""One command to wire Claude Code into Clover.

Users reported that connecting Claude Code was complicated, and they were
right. The old path asked them to type a provider name at a blank prompt,
from a list of 80. None of those 80 names is "Claude Code". The answer was
"anthropic", which the screen never said.

Their Claude Code login already sits at ~/.claude/.credentials.json. Clover
knew that path and read it when checking for changes, but setup never offered
to use it. So this command finds it, points Clover at it, and picks a model.

Deliberately does NOT copy the token anywhere. Clover reads Claude Code's own
file, so a `claude login` refresh keeps working with no action here.
"""
import json
import os
import time
from pathlib import Path

CRED = Path.home() / ".claude" / ".credentials.json"
DEFAULT_MODEL = "claude-opus-5"
FALLBACKS = ["claude-sonnet-4-6", "claude-haiku-4-5"]


def read_login(path: Path = CRED) -> dict | None:
    """Return the Claude Code login, or None with a reason printed.

    Never raises: a missing or malformed file is a normal state for someone
    who has not installed Claude Code yet, and it deserves an instruction
    rather than a traceback.
    """
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    oauth = blob.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    return oauth


def describe(oauth: dict) -> list[str]:
    """Human-readable facts about the login, for the confirmation screen."""
    out = []
    plan = oauth.get("subscriptionType")
    if plan:
        out.append(f"plan: {plan}")
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)):
        # expiresAt is milliseconds. A past value is not fatal: Clover
        # refreshes with the refresh token, so report it without alarm.
        left = exp / 1000 - time.time()
        if left > 0:
            out.append(f"token valid for {int(left // 3600)}h")
        else:
            out.append("token expired, Clover will refresh it")
    return out


def apply(model: str = DEFAULT_MODEL) -> None:
    """Point Clover at Claude Code and set the model.

    Two writes, both through Clover's own helpers so this stays correct if
    the storage format changes:

      use_anthropic_claude_code_credentials()  clears the env token slots, so
        the reader falls through to Claude Code's own file
      save_config()                            sets provider and model
    """
    from clover_cli.config import (
        load_config,
        save_config,
        use_anthropic_claude_code_credentials,
    )

    use_anthropic_claude_code_credentials()

    cfg = load_config() or {}
    model_cfg = cfg.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    model_cfg["provider"] = "anthropic"
    model_cfg["default"] = model
    cfg["model"] = model_cfg

    # A single dead provider leaves the agent with nothing to answer with.
    # Give it the same shape Alfred and Oracle run: same provider, cheaper
    # models underneath.
    if not cfg.get("fallback_providers"):
        cfg["fallback_providers"] = [
            {"provider": "anthropic", "model": m} for m in FALLBACKS
        ]

    save_config(cfg)


def command(args=None) -> int:
    """`clover setup claude` — the whole flow, start to finish."""
    print()
    print("  Connecting Claude Code")
    print("  ---------------------")
    print()

    oauth = read_login()
    if oauth is None:
        print("  No Claude Code login found.")
        print()
        print(f"  Looked in: {CRED}")
        print()
        print("  Install Claude Code and sign in, then run this again:")
        print("    npm install -g @anthropic-ai/claude-code")
        print("    claude login")
        print()
        return 1

    print(f"  Found your Claude Code login at {CRED}")
    for line in describe(oauth):
        print(f"    {line}")
    print()

    model = getattr(args, "model", None) or DEFAULT_MODEL
    try:
        apply(model)
    except Exception as exc:
        print(f"  Could not save the settings: {exc}")
        return 1

    print("  Done. Clover now uses your Claude Code subscription.")
    print()
    print(f"    provider  anthropic")
    print(f"    model     {model}")
    print(f"    fallback  {', '.join(FALLBACKS)}")
    print()
    print("  Clover reads Claude Code's own credential file, so a future")
    print("  `claude login` keeps working with nothing to redo here.")
    print()
    print("  Start talking:  clover")
    print()
    return 0


def add_parser(subparsers) -> None:
    """Register `clover setup claude`."""
    p = subparsers.add_parser(
        "claude",
        help="Connect Claude Code in one step (uses your existing login)",
        description=(
            "Find the Claude Code login already on this machine, point Clover "
            "at it, and set a model. Nothing to copy or paste."
        ),
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model to use (default: {DEFAULT_MODEL})",
    )
    p.set_defaults(func=command)
