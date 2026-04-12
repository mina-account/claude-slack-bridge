"""
session_store.py — SQLite-backed persistence for Slack thread sessions.

Persists two mappings that would otherwise be lost on daemon restart:
  - thread_ts → channel_id  (needed to route HTTP callbacks)
  - thread_ts → session_id  (needed to resume the Claude CLI session)

Uses an in-memory cache (warmed at startup) so all reads are O(1) dict
lookups; SQLite is only hit on writes and initial load.
"""

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_ts   TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    session_id  TEXT,
    updated_at  REAL NOT NULL
)
"""


class SessionStore:
    """
    Thin SQLite wrapper that keeps an in-memory cache in sync with disk.

    Args:
        db_path: Filesystem path for the SQLite database file.
                 Parent directory is created automatically if missing.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._channels: dict[str, str] = {}       # thread_ts → channel_id
        self._sessions: dict[str, str | None] = {}  # thread_ts → session_id

    def initialize(self) -> None:
        """Create the DB schema and warm the in-memory cache from disk."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_DDL)
            rows = conn.execute(
                "SELECT thread_ts, channel_id, session_id FROM sessions"
            ).fetchall()

        for thread_ts, channel_id, session_id in rows:
            self._channels[thread_ts] = channel_id
            self._sessions[thread_ts] = session_id

        logger.info("SessionStore initialised — loaded %d sessions from %s.", len(rows), self._db_path)

    def upsert(self, thread_ts: str, channel_id: str, session_id: str | None) -> None:
        """Persist (or update) a thread mapping and refresh the cache."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (thread_ts, channel_id, session_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(thread_ts) DO UPDATE SET
                    channel_id  = excluded.channel_id,
                    session_id  = excluded.session_id,
                    updated_at  = excluded.updated_at
                """,
                (thread_ts, channel_id, session_id, time.time()),
            )
        self._channels[thread_ts] = channel_id
        self._sessions[thread_ts] = session_id
        logger.debug("SessionStore upserted thread_ts=%s channel=%s session=%s", thread_ts, channel_id, session_id)

    def get_channel(self, thread_ts: str) -> str | None:
        """Return the channel ID for a known thread, or None."""
        return self._channels.get(thread_ts)

    def get_session_id(self, thread_ts: str) -> str | None:
        """Return the Claude CLI session UUID for a known thread, or None."""
        return self._sessions.get(thread_ts)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
