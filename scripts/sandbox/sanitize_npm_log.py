#!/usr/bin/env python3
"""Extract non-sensitive npm failure facts from debug logs.

Only an npm error code and the hostname from a FetchError request are emitted.
No raw lines, URL paths, query strings, headers, credentials, or local paths are
copied to CI artifacts.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

_ERROR_CODE = re.compile(r"\berror code\s+([A-Za-z0-9_.-]+)", re.IGNORECASE)
_REQUEST_URL = re.compile(r"request to\s+(https?://[^\s\"'<>]+)", re.IGNORECASE)


def sanitize_debug_text(text: str) -> str:
    """Return at most an npm error code and a request hostname."""
    code = None
    host = None
    for line in text.splitlines():
        if code is None:
            match = _ERROR_CODE.search(line)
            if match:
                code = match.group(1)
        if host is None and "fetcherror" in line.lower():
            match = _REQUEST_URL.search(line)
            if match:
                try:
                    host = urlsplit(match.group(1)).hostname
                except ValueError:
                    host = None
        if code is not None and host is not None:
            break

    facts = []
    if code is not None:
        facts.append(f"npm_error_code={code}")
    if host:
        facts.append(f"npm_error_host={host.lower()}")
    return "\n".join(facts) if facts else "npm_debug_failure_details=unavailable"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_dir", type=Path, help="directory containing npm debug logs")
    args = parser.parse_args()

    paths = sorted(args.log_dir.glob("*-debug-0.log")) if args.log_dir.is_dir() else []
    summaries = [sanitize_debug_text(path.read_text(encoding="utf-8", errors="replace")) for path in paths]
    print("\n".join(dict.fromkeys(summaries)) if summaries else "npm_debug_log=missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
