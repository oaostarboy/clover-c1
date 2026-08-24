#!/usr/bin/env python3
"""Rename a migrated agent, without breaking it.

WHY THIS IS NOT sed
A migrated agent's files mix three different kinds of "Aria":

  1. IDENTITY   "You are **Aria**, the Marketing Director"  -> rename
  2. PATHS      "~/.openclaw-aria/workspace"                -> DO NOT TOUCH
  3. SECRETS    "agent-hooks-2026"                           -> DO NOT TOUCH

A blind find-and-replace renames all three. The agent then believes it is
Oracle, and simultaneously points at directories that do not exist and
presents a token that no longer matches. It comes up looking fine and fails
on first real use, which is the worst possible failure shape.

So: rename PROSE, leave PATHS and TOKENS exactly as written. Historical notes
("Aria agent fully operational on the gateway host, 2026-02-21") are left alone
too — that is a true record of something that happened under that name.

Usage:
    rename_agent.py --root /path/to/clover-home --from Aria --to Oracle
    rename_agent.py ... --execute      # default is a dry run
"""
import argparse
import re
import sys
from pathlib import Path

# A match is PROTECTED if the line looks like a path, a token, an env var, or
# a URL. Checked against the whole line, because that is where the context is.
PROTECTED_LINE = re.compile(
    r"""(
        [~/][\w./-]*            # a filesystem path anywhere in the line
      | \.env\b
      | [A-Z0-9_]{3,}=          # ENV_VAR=
      | https?://
      | -(?:hooks|token|key|secret|gw)-   # agent-hooks-2026 style
      | \bport\b
    )""",
    re.X | re.I,
)

# Lines that are unambiguously the agent's own identity.
IDENTITY_HINT = re.compile(
    r"(you are|^#|name\*{0,2}\s*[:=]|\bi am\b|marketing director|assistant)",
    re.I,
)


def plan_line(line: str, old: str, new: str) -> tuple[str, str]:
    """Return (new_line, reason)."""
    if old.lower() not in line.lower():
        return line, ""
    if PROTECTED_LINE.search(line):
        return line, "protected (path/token/env/url)"
    if not IDENTITY_HINT.search(line):
        return line, "left alone (historical or ambiguous)"
    pattern = re.compile(re.escape(old), re.I)

    def _sub(m):
        s = m.group(0)
        if s.isupper():
            return new.upper()
        if s[0].isupper():
            return new[0].upper() + new[1:]
        return new.lower()

    return pattern.sub(_sub, line), "renamed"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--from", dest="old", required=True)
    ap.add_argument("--to", dest="new", required=True)
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()

    root = Path(a.root)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    targets = [p for p in root.rglob("*")
               if p.is_file() and p.suffix.lower() in {".md", ".yaml", ".yml", ".txt"}]
    renamed = protected = untouched = 0
    for path in sorted(targets):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if a.old.lower() not in text.lower():
            continue
        out, changed_here = [], False
        for line in text.splitlines():
            new_line, reason = plan_line(line, a.old, a.new)
            if reason == "renamed":
                renamed += 1
                changed_here = True
                print(f"  RENAME  {path.relative_to(root)}: {line.strip()[:70]}")
            elif reason == "protected (path/token/env/url)":
                protected += 1
                print(f"  KEEP    {path.relative_to(root)}: {line.strip()[:70]}")
            elif reason:
                untouched += 1
            out.append(new_line)
        if changed_here and a.execute:
            path.write_text("\n".join(out) + ("\n" if text.endswith("\n") else ""),
                            encoding="utf-8")

    print(f"\n  renamed:   {renamed}")
    print(f"  protected: {protected}  (paths/tokens left literal on purpose)")
    print(f"  untouched: {untouched}  (historical or ambiguous)")
    if not a.execute:
        print("\n  DRY RUN — pass --execute to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
