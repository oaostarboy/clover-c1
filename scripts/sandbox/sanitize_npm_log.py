#!/usr/bin/env python3
"""Extract non-sensitive npm failure facts from debug logs.

Only an npm error code, the package whose lifecycle script failed, the first
scrubbed node-gyp error line, and the hostname from a FetchError request are
emitted.  No raw lines, URL paths, query
strings, headers, credentials, or local paths are copied to CI artifacts.

``error code 1`` (a bare number) is a lifecycle script's exit status rather than
a network code; the package name is what makes it actionable.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

_ERROR_CODE = re.compile(r"\berror code\s+([A-Za-z0-9_.-]+)", re.IGNORECASE)
_ERROR_PATH = re.compile(r"\berror path\s+(\S+)", re.IGNORECASE)
_PACKAGE_NAME = re.compile(r"node_modules/((?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+)/?$")
# node-gyp reports why a native build failed on `gyp ERR!` lines.  Keep only the
# first error line, with every URL and path-like token replaced, so the reason
# ("not found: make", "Could not find any Python installation") survives
# without leaking where the build ran.
_GYP_ERROR = re.compile(r"gyp ERR!\s+(?:stack\s+)?(.*(?:not found|Error|error|ENOENT|EAI_AGAIN|ECONN\w+|EPROTO|CERT\w*).*)")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_PATHLIKE = re.compile(r"(?<![\w.-])(?:/|[A-Za-z]:[\\/])[^\s:;,'\"()]+")
_REQUEST_URL = re.compile(r"request to\s+(https?://[^\s\"'<>]+)", re.IGNORECASE)


def sanitize_debug_text(text: str) -> str:
    """Return at most an npm error code, a failing package and a request host."""
    code = None
    host = None
    package = None
    gyp = None
    for line in text.splitlines():
        if gyp is None:
            match = _GYP_ERROR.search(line)
            if match:
                gyp = _PATHLIKE.sub("<path>", _URL.sub("<url>", match.group(1))).strip()[:160]
        if code is None:
            match = _ERROR_CODE.search(line)
            if match:
                code = match.group(1)
        if package is None:
            match = _ERROR_PATH.search(line)
            if match:
                # Keep only the public package name, never the local prefix.
                named = _PACKAGE_NAME.search(match.group(1).replace("\\", "/"))
                if named:
                    package = named.group(1)
        if host is None and "fetcherror" in line.lower():
            match = _REQUEST_URL.search(line)
            if match:
                try:
                    host = urlsplit(match.group(1)).hostname
                except ValueError:
                    host = None

    facts = []
    if code is not None:
        facts.append(f"npm_error_code={code}")
    if package:
        facts.append(f"npm_error_package={package}")
    if gyp:
        facts.append(f"node_gyp_error={gyp}")
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
