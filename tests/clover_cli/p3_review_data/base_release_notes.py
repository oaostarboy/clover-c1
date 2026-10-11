"""Version name + "what's new" for the message users get after ``/update``.

The notes live in a human-edited file at the repo root, ``RELEASE_NOTES.md``
(one ``## <version> | <release name> | <date>`` section per release, 3-6 plain
bullets each; the file's header comment explains how to edit it).  This module
parses it, picks the entries between the version the user updated *from* and the
version now running, and renders a short block for the existing "Clover update
finished" chat message.  It adds no message of its own.

Design points (each one is a real failure class for ``/update``):

* **The restarted gateway renders it.**  The old updater/gateway cannot know
  about this feature, so the first hop from an older release works only because
  the *new* gateway (restarted onto new code) reads the notes from disk and
  renders them.  The "from" version is therefore recovered from what an old
  updater already leaves behind (this run's update receipt), or, for gateways
  that have this feature, from the pending marker written at ``/update`` time.
* **Never reuse an old receipt.**  A receipt only counts when it started after
  this run's pending marker was written.
* **Never fail an update report.**  Every public function swallows errors and
  degrades to less text (down to an empty string, i.e. today's message).
* **Draft entries never leak.**  A section containing ``<!-- draft -->`` keeps
  its name/version but its bullets are never shown.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

NOTES_FILENAME = "RELEASE_NOTES.md"

# Telegram-friendly: a whole block of at most this many lines (header included).
MAX_SECTION_LINES = 10
MAX_BULLET_CHARS = 110
_MAX_NAME_CHARS = 60
_MAX_FILE_BYTES = 256 * 1024
# Receipt started_at vs. marker mtime: tolerate coarse filesystem timestamps.
_RECEIPT_SLACK_SECONDS = 5.0

_DRAFT_SENTINEL = "\x00DRAFT\x00"
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DRAFT_RE = re.compile(r"<!--\s*draft\s*-->", re.IGNORECASE)
_HEADER_RE = re.compile(r"^##\s+(?P<version>[^|\s]+)\s*\|\s*(?P<name>[^|]*?)\s*\|\s*(?P<date>.*?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<text>\S.*?)\s*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    name: str
    date: str
    bullets: tuple[str, ...]
    draft: bool = False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_version(value: object) -> Optional[tuple[int, ...]]:
    """``"1.1.1"`` / ``"v1.1.1"`` -> ``(1, 1, 1)``; anything else -> ``None``.

    Trailing zero components are ignored for comparison, so ``1.1`` == ``1.1.0``.
    """
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    if not match:
        return None
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _clean(text: str, limit: int) -> str:
    text = _CONTROL_RE.sub("", text).strip()
    if len(text) > limit:
        text = text[: max(limit - 1, 1)].rstrip() + "…"
    return text


def parse_release_notes(text: str) -> list[ReleaseEntry]:
    """Parse the notes file into entries, in file order.  Never raises.

    Malformed headers (unparseable version) and duplicate versions are skipped
    (first one wins).  Bullets that start with ``TODO`` are placeholders and are
    dropped, and an entry whose only content was placeholders is a draft.
    """
    try:
        text = _DRAFT_RE.sub(_DRAFT_SENTINEL, text or "")
        text = _COMMENT_RE.sub("", text)
        entries: list[ReleaseEntry] = []
        seen: set[tuple[int, ...]] = set()
        current: Optional[dict] = None

        def _flush() -> None:
            nonlocal current
            if current is None:
                return
            key = parse_version(current["version"])
            if key is not None and key not in seen:
                seen.add(key)
                entries.append(
                    ReleaseEntry(
                        version=current["version"],
                        name=current["name"],
                        date=current["date"],
                        bullets=tuple(current["bullets"]),
                        draft=bool(current["draft"]),
                    )
                )
            current = None

        for raw in text.splitlines():
            line = raw.rstrip()
            if line.startswith("## "):
                _flush()
                header = _HEADER_RE.match(line)
                if header and parse_version(header.group("version")) is not None:
                    version = header.group("version").lstrip("vV")
                    current = {
                        "version": _clean(version, 32),
                        "name": _clean(header.group("name"), _MAX_NAME_CHARS),
                        "date": _clean(header.group("date"), 32),
                        "bullets": [],
                        "draft": False,
                    }
                continue
            if current is None:
                continue
            if _DRAFT_SENTINEL in line:
                current["draft"] = True
                continue
            bullet = _BULLET_RE.match(line)
            if bullet:
                body = _clean(bullet.group("text"), MAX_BULLET_CHARS)
                if body.upper().startswith("TODO"):
                    current["draft"] = True
                elif body:
                    current["bullets"].append(body)
        _flush()
        return entries
    except Exception:  # pragma: no cover - defensive; parsing must not break /update
        return []


def default_notes_path() -> Path:
    return Path(__file__).resolve().parent.parent / NOTES_FILENAME


def load_release_notes(path: Optional[Path] = None) -> list[ReleaseEntry]:
    """Read + parse the notes file.  Missing/unreadable/oversized -> ``[]``."""
    try:
        target = Path(path) if path is not None else default_notes_path()
        if not target.is_file() or target.stat().st_size > _MAX_FILE_BYTES:
            return []
        return parse_release_notes(target.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Selection + rendering
# ---------------------------------------------------------------------------


def entries_between(
    entries: Iterable[ReleaseEntry], from_version: object, to_version: object
) -> list[ReleaseEntry]:
    """Entries with ``from_version < version <= to_version``, newest first."""
    lo = parse_version(from_version)
    hi = parse_version(to_version)
    if lo is None or hi is None:
        return []
    picked = []
    for entry in entries:
        key = parse_version(entry.version)
        if key is not None and lo < key <= hi:
            picked.append((key, entry))
    picked.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in picked]


def find_entry(entries: Iterable[ReleaseEntry], version: object) -> Optional[ReleaseEntry]:
    key = parse_version(version)
    if key is None:
        return None
    for entry in entries:
        if parse_version(entry.version) == key:
            return entry
    return None


def release_label(entries: Iterable[ReleaseEntry], version: object) -> str:
    """``"Clover C1.1.1 (v1.1.1)"``; without a notes entry, ``"Clover v1.1.1"``."""
    ver = str(version or "").strip().lstrip("vV")
    entry = find_entry(entries, ver)
    if entry is not None and entry.name:
        return f"{entry.name} (v{entry.version})"
    return f"Clover v{ver}" if ver else "Clover"


def format_update_section(
    *,
    entries: list[ReleaseEntry],
    to_version: object,
    from_version: object = None,
    from_sha: Optional[str] = None,
    to_sha: Optional[str] = None,
    pulled_new_code: bool = False,
    origin_trusted: bool = True,
    max_lines: int = MAX_SECTION_LINES,
) -> str:
    """Render the block appended to the "update finished" message.

    ``from_sha``/``to_sha`` are the code identity before the update and of the
    code now running; equal shas mean no new code arrived.  ``pulled_new_code``
    is True when this run's receipt shows the checkout moved to new code.
    ``origin_trusted`` is False when the starting point came only from a
    receipt: a receipt written by a re-exec'd updater child (Windows shim
    hand-off) already sees the NEW checkout as its "before", so equal shas
    there cannot prove nothing was pulled and "already up to date" must not
    be claimed.  Returns ``""`` when there is no running version to report.
    """
    to_ver = str(to_version or "").strip().lstrip("vV")
    if not to_ver:
        return ""
    label = release_label(entries, to_ver)
    lo = parse_version(from_version)
    hi = parse_version(to_ver)

    same_code = bool(from_sha and to_sha and from_sha == to_sha)
    if same_code:
        if pulled_new_code:
            # The checkout moved but this process still runs the old code.
            return (
                f"Running {label}. New code was downloaded; send /restart "
                "to switch to it."
            )
        if not origin_trusted:
            return f"You are on {label}."
        return f"Already up to date. You are on {label}."

    if lo is None or hi is None or lo >= hi:
        # No usable range (unknown start, same version, or a rollback): name the
        # version only; repeating old notes would be noise.
        unchanged = lo is not None and lo == hi and not (from_sha and to_sha)
        return f"{'You are on' if unchanged else 'Now on'} {label}."

    from_text = f"v{str(from_version).strip().lstrip('vV')}"
    header = f"Now on {label}, updated from {from_text}."
    selected = [e for e in entries_between(entries, from_version, to_ver) if not e.draft and e.bullets]
    if not selected:
        return header

    multi = len(selected) > 1
    body: list[tuple[str, bool]] = []  # (line, is_bullet)
    for entry in selected:
        if multi:
            body.append((f"{entry.name or 'Clover'} (v{entry.version}):", False))
        for bullet in entry.bullets:
            body.append((f"• {bullet}", True))

    room = max(max_lines - 2, 1)  # header + "What's new:"
    if len(body) > room:
        keep = max(room - 1, 1)  # one line reserved for "and N more"
        kept = body[:keep]
        # Never end on a dangling per-version heading.
        while kept and not kept[-1][1]:
            kept.pop()
        hidden = sum(1 for _, is_bullet in body if is_bullet) - sum(1 for _, b in kept if b)
        lines = [line for line, _ in kept]
        if hidden > 0:
            lines.append(f"…and {hidden} more")
    else:
        lines = [line for line, _ in body]
    return "\n".join([header, "What's new:", *lines])


# ---------------------------------------------------------------------------
# Where did this run start from?  (marker first, then this run's receipt)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _receipt_started_ts(receipt: dict) -> Optional[float]:
    started = receipt.get("started_at")
    if not isinstance(started, str) or not started:
        return None
    try:
        return datetime.fromisoformat(started).timestamp()
    except ValueError:
        return None


def read_this_runs_receipt(clover_home: Path, marker_mtime: Optional[float]) -> Optional[dict]:
    """Latest update receipt, but only if it belongs to THIS ``/update`` run.

    The pending marker is written before the updater is spawned, so a receipt of
    this run started after it.  No readable marker time -> no proof -> ``None``;
    an earlier update's receipt must never be reported as this run's.
    """
    if not marker_mtime or marker_mtime <= 0:
        return None
    receipt = _read_json(Path(clover_home) / "logs" / "update_receipts" / "latest.json")
    if receipt is None:
        return None
    started = _receipt_started_ts(receipt)
    if started is None or started < marker_mtime - _RECEIPT_SLACK_SECONDS:
        return None
    return receipt


def current_code_identity() -> tuple[Optional[str], Optional[str]]:
    """``(version, sha)`` of the code THIS process runs.  Never raises."""
    version: Optional[str] = None
    sha: Optional[str] = None
    try:
        from clover_cli import __version__

        version = __version__
    except Exception:
        pass
    try:
        from clover_cli.build_info import get_code_identity

        sha = get_code_identity().get("sha")
    except Exception:
        pass
    return version, sha


def read_marker(paths: Iterable[Path]) -> tuple[Optional[dict], Optional[float]]:
    """First readable ``/update`` pending marker: ``(contents, mtime)``."""
    for path in paths:
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            continue
        return _read_json(Path(path)), mtime
    return None, None


def pending_marker_origin() -> dict:
    """Fields stored in the ``/update`` pending marker so the restarted gateway
    can tell where this run started from.  Never raises."""
    version, sha = current_code_identity()
    out: dict = {}
    if version:
        out["from_version"] = version
    if sha:
        out["from_sha"] = sha
    return out


def build_post_update_section(
    clover_home: Path,
    pending: Optional[dict],
    marker_mtime: Optional[float],
    *,
    notes_path: Optional[Path] = None,
    running_version: Optional[str] = None,
    running_sha: Optional[str] = None,
) -> str:
    """The text to append to a *successful* ``/update`` completion message.

    ``pending`` is the parsed pending marker, ``marker_mtime`` its mtime.  The
    running version/sha default to this process's own (which, in a restarted
    gateway, is the new code).  Returns ``""`` on any problem so the caller's
    existing message is sent unchanged.
    """
    try:
        if running_version is None or running_sha is None:
            cur_version, cur_sha = current_code_identity()
            running_version = running_version or cur_version
            running_sha = running_sha or cur_sha
        if not running_version:
            return ""
        entries = load_release_notes(notes_path)

        from_version: Optional[str] = None
        from_sha: Optional[str] = None
        pulled_new_code = False
        origin_trusted = False

        receipt = read_this_runs_receipt(Path(clover_home), marker_mtime)
        if receipt is not None:
            pre: dict = receipt.get("pre_update") or {}
            post: dict = receipt.get("post_update") or {}
            if not isinstance(pre, dict):
                pre = {}
            if not isinstance(post, dict):
                post = {}
            from_version = pre.get("version") or None
            from_sha = pre.get("sha") or None
            post_sha = post.get("sha") or None
            pulled_new_code = bool(from_sha and post_sha and from_sha != post_sha)

        # A gateway that has this feature recorded its own running version when
        # /update was sent.  That is the version users actually ran, so it wins
        # over the receipt (e.g. a previous pull that never restarted the bot).
        if isinstance(pending, dict):
            if pending.get("from_version"):
                from_version = str(pending["from_version"])
            if pending.get("from_sha"):
                from_sha = str(pending["from_sha"])
                origin_trusted = True

        return format_update_section(
            entries=entries,
            to_version=running_version,
            from_version=from_version,
            from_sha=from_sha,
            to_sha=running_sha,
            pulled_new_code=pulled_new_code,
            origin_trusted=origin_trusted,
        )
    except Exception:
        return ""


def section_for_update_markers(
    clover_home: Path,
    marker_paths: Iterable[Path],
    *,
    notes_path: Optional[Path] = None,
) -> str:
    """Convenience for the gateway: read the first existing pending marker
    (``.update_pending.claimed.json`` then ``.update_pending.json``) and build
    the section.  Call it BEFORE the markers are deleted.  Never raises."""
    try:
        pending, mtime = read_marker(marker_paths)
        return build_post_update_section(
            Path(clover_home), pending, mtime, notes_path=notes_path
        )
    except Exception:
        return ""
