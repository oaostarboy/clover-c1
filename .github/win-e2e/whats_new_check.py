"""Check the final /update completion message captured by the fake IRC server.

Reads the fake ircd log (see fake_ircd.py): every line the gateway sent is
logged as "<time> << PRIVMSG <nick> :<text>".  After the trigger, the gateway's
completion message is one send; IRC delivers each line of it as its own PRIVMSG
(blank lines dropped), so the message is the run of lines from the
"Clover update finished" line to the end of the log.

Usage:
  python whats_new_check.py <irc.log> <start_line> notes|uptodate \
      --version 1.1.1 --name "Clover C1.1.1" --from 1.1.0 --bullet "..." \
      [--wait 600] [--out final-message.txt]

Exit 0 on pass, 1 on fail.  Stdlib only; runs on the runner's system Python.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

MSG_RE = re.compile(r"^\S+ << PRIVMSG \S+ :(.*)$")


def bot_lines(path: str, start: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            rows = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for row in rows[start:]:
        match = MSG_RE.match(row)
        if match:
            out.append(match.group(1))
    return out


def completion_lines(lines: list[str]) -> tuple[int, list[str]]:
    finished = [i for i, text in enumerate(lines) if "Clover update finished" in text]
    if not finished:
        return 0, []
    return len(finished), lines[finished[-1]:]


def main() -> int:
    # Windows consoles default to cp1252; the message has emoji and bullets.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("start", type=int)
    ap.add_argument("mode", choices=["notes", "uptodate"])
    ap.add_argument("--version", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--from", dest="from_version", default="")
    ap.add_argument("--bullet", action="append", default=[])
    ap.add_argument("--wait", type=int, default=600)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    deadline = time.time() + args.wait
    count, block = 0, []
    while time.time() < deadline:
        count, block = completion_lines(bot_lines(args.log, args.start))
        if count:
            time.sleep(8)  # let the rest of the message's lines arrive
            count, block = completion_lines(bot_lines(args.log, args.start))
            break
        time.sleep(3)

    text = "\n".join(block)
    print("----- final completion message as delivered (one line per IRC PRIVMSG) -----")
    print(text if text else "(none)")
    print("---------------------------------------------------------------------------")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    label = f"{args.name} (v{args.version})"
    problems: list[str] = []
    if count != 1:
        problems.append(f"expected exactly 1 'Clover update finished' message, saw {count}")
    if label not in text:
        problems.append(f"missing release label {label!r}")
    if args.mode == "notes":
        if "What's new:" not in text:
            problems.append("missing \"What's new:\" section")
        if args.from_version and f"updated from v{args.from_version}" not in text:
            problems.append(f"missing 'updated from v{args.from_version}'")
        for bullet in args.bullet:
            if f"• {bullet}" not in text:
                problems.append(f"missing bullet {bullet!r}")
        if "TODO" in text or "placeholder" in text.lower():
            problems.append("placeholder/draft text leaked into the message")
        notes_lines = [l for l in block if l.startswith("•") or l.startswith("What's new") or l.startswith("Now on") or l.startswith("…and")]
        if len(notes_lines) > 11:
            problems.append(f"notes block too long for chat: {len(notes_lines)} lines")
    else:
        if "Already up to date" not in text:
            problems.append("missing 'Already up to date'")
        if "What's new" in text or "•" in text:
            problems.append("notes were repeated on an up-to-date /update")

    if problems:
        print("FAIL:")
        for item in problems:
            print("  -", item)
        return 1
    print(f"PASS ({args.mode}): {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
