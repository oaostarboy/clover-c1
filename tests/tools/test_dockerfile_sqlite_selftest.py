"""The Dockerfile's build-time SQLite FTS5 trigram self-test must be satisfiable.

The self-test inserts one row and queries a trigram MATCH. The query must be a
substring of the inserted text (>= 3 chars), otherwise the image build fails
on every SQLite regardless of version.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_INSERT = re.compile(r"INSERT INTO docs VALUES \('([^']+)'\)")
_MATCH = re.compile(r"docs MATCH '([^']+)'")


def _has_fts5_trigram() -> bool:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE t USING fts5(c, tokenize='trigram')")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        db.close()


def _extract(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    inserted = _INSERT.search(text)
    matched = _MATCH.search(text)
    assert inserted and matched, f"self-test statements not found in {path.name}"
    return inserted.group(1), matched.group(1)


@pytest.mark.skipif(not _has_fts5_trigram(), reason="SQLite lacks FTS5 trigram")
@pytest.mark.parametrize(
    "relpath",
    ["Dockerfile", "tests/docker/test_sqlite_runtime.py"],
)
def test_trigram_selftest_query_matches_inserted_row(relpath: str) -> None:
    content, query = _extract(REPO_ROOT / relpath)
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')")
        db.execute("INSERT INTO docs VALUES (?)", (content,))
        count = db.execute(
            "SELECT count(*) FROM docs WHERE docs MATCH ?", (query,)
        ).fetchone()[0]
    finally:
        db.close()
    assert count == 1, f"{relpath}: MATCH {query!r} finds {count} rows in {content!r}"
