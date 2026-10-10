#!/usr/bin/env python3
"""Extract non-sensitive npm failure facts from debug logs.

Output is an ALLOWLIST of facts, never log text: an exit status or errno-style
code, the name of the package whose lifecycle script failed (only if the repo
lockfile pins it), a fixed
label for a recognised node-gyp cause, and the hostname of a FetchError request.
No raw line, URL path, query string, header, credential or local path is ever
copied to CI artifacts, so there is nothing to scrub incorrectly.

``error code 1`` (a bare number) is a lifecycle script's exit status rather than
a network code; the package name is what makes it actionable.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

# The code is either a bare exit status or one of a CLOSED set of well-known
# errno / TLS names.  Any other token on the line is log text and is dropped.
_KNOWN_CODES = frozenset({
    "ECONNRESET", "ECONNREFUSED", "ECONNABORTED", "ETIMEDOUT", "ENOTFOUND",
    "EAI_AGAIN", "EPROTO", "EACCES", "EPERM", "ENOENT", "ENOSPC", "EEXIST",
    "EBADENGINE", "E401", "E403", "E404", "E429", "E500", "E502", "E503",
    "CERT_HAS_EXPIRED", "UNABLE_TO_VERIFY_LEAF_SIGNATURE", "SELF_SIGNED_CERT_IN_CHAIN",
    "DEPTH_ZERO_SELF_SIGNED_CERT", "ERR_SSL_WRONG_VERSION_NUMBER",
    "ERR_SOCKET_TIMEOUT", "ERR_INVALID_URL",
})
_ERROR_CODE = re.compile(r"\berror code\s+(\d{1,3}|[A-Z][A-Z0-9_]{1,40})\s*$")
_ERROR_PATH = re.compile(r"\berror path\s+(\S+)", re.IGNORECASE)
_PACKAGE_NAME = re.compile(r"node_modules/((?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+)/?$")
# node-gyp explains a failed native build on `gyp ERR!` lines, but those lines
# are free text that can embed anything (headers, tokens, paths).  Never copy
# them.  Instead recognise a closed set of known causes and emit only the fixed
# label for the first one found.
_GYP_LINE = re.compile(r"\bgyp ERR!", re.IGNORECASE)
_GYP_CAUSES = (
    (re.compile(r"not found:\s*make\b", re.IGNORECASE), "make_not_found"),
    (re.compile(r"not found:\s*(?:g\+\+|gcc|cc|c\+\+)\b", re.IGNORECASE), "compiler_not_found"),
    (re.compile(r"Could not find any Python", re.IGNORECASE), "python_not_found"),
    (re.compile(r"common\.gypi not found", re.IGNORECASE), "node_headers_not_found"),
    (re.compile(r"\bEAI_AGAIN\b"), "dns_failure"),
    (re.compile(r"\bECONNRESET\b"), "connection_reset"),
    (re.compile(r"\bECONNREFUSED\b"), "connection_refused"),
    (re.compile(r"\bETIMEDOUT\b"), "timed_out"),
    (re.compile(r"\b(?:CERT_\w+|UNABLE_TO_VERIFY_LEAF_SIGNATURE|SELF_SIGNED_CERT\w*)\b"), "tls_certificate"),
    (re.compile(r"\bEPROTO\b|SSL", re.IGNORECASE), "tls_protocol"),
    (re.compile(r"\bENOENT\b"), "file_not_found"),
    (re.compile(r"\bEACCES\b|\bEPERM\b"), "permission_denied"),
)
_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_REQUEST_URL = re.compile(r"request to\s+(https?://[^\s\"'<>]+)", re.IGNORECASE)


def known_packages_from_lock(lock_path: Path) -> "frozenset[str]":
    """Names of packages the repo itself pins; the only names ever printed."""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    names = set()
    for key in (data.get("packages") or {}):
        if "node_modules/" in key:
            names.add(key.rsplit("node_modules/", 1)[1])
    return frozenset(names)


def sanitize_debug_text(text: str, known_packages: "frozenset[str] | set[str]" = frozenset()) -> str:
    """Return only allowlisted facts: code, package, gyp cause label, request host."""
    code = None
    host = None
    package = None
    gyp = None
    for line in text.splitlines():
        if gyp is None and _GYP_LINE.search(line):
            for pattern, label in _GYP_CAUSES:
                if pattern.search(line):
                    gyp = label
                    break
        if code is None:
            match = _ERROR_CODE.search(line)
            if match and (match.group(1).isdigit() or match.group(1) in _KNOWN_CODES):
                code = match.group(1)
        if package is None:
            match = _ERROR_PATH.search(line)
            if match:
                # Keep only the public package name, never the local prefix.
                named = _PACKAGE_NAME.search(match.group(1).replace("\\", "/"))
                # A package name is log text too: print it only if the repo's
                # own lockfile pins that exact name.
                if named and named.group(1) in known_packages:
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
        facts.append(f"node_gyp_cause={gyp}")
    if host and _HOSTNAME.fullmatch(host):
        facts.append(f"npm_error_host={host.lower()}")
    return "\n".join(facts) if facts else "npm_debug_failure_details=unavailable"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_dir", type=Path, help="directory containing npm debug logs")
    parser.add_argument(
        "--lockfile",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "package-lock.json",
        help="package-lock.json whose package names may be reported",
    )
    args = parser.parse_args()
    known = known_packages_from_lock(args.lockfile)

    paths = sorted(args.log_dir.glob("*-debug-0.log")) if args.log_dir.is_dir() else []
    summaries = [sanitize_debug_text(path.read_text(encoding="utf-8", errors="replace"), known) for path in paths]
    print("\n".join(dict.fromkeys(summaries)) if summaries else "npm_debug_log=missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
