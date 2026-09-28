"""Persistent map between Telegram message IDs and Max message IDs.

Needed to carry replies, edits and deletions across the bridge in both
directions. Backed by SQLite (stdlib) in the state directory; old rows are
pruned so the file stays small.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

log = logging.getLogger(__name__)

# Keep roughly this many most recent mappings.
MAX_ROWS = 50_000
_PRUNE_EVERY = 500


class MessageMap:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS msgmap (
                   max_chat_id TEXT NOT NULL,
                   max_msg_id  TEXT NOT NULL,
                   tg_msg_id   INTEGER NOT NULL,
                   text        TEXT,
                   ts          REAL NOT NULL
               )"""
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_max ON msgmap(max_chat_id, max_msg_id)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_tg ON msgmap(tg_msg_id)")
        self._db.commit()
        self._writes = 0

    def add(self, max_chat_id: Any, max_msg_id: Any, tg_msg_id: int,
            text: str | None = None) -> None:
        """Link a Max message to the Telegram message that mirrors it."""
        if max_msg_id in (None, "") or tg_msg_id is None:
            return
        try:
            self._db.execute(
                "INSERT INTO msgmap VALUES (?, ?, ?, ?, ?)",
                (str(max_chat_id), str(max_msg_id), int(tg_msg_id), text, time.time()),
            )
            self._db.commit()
        except Exception:
            log.exception("MessageMap.add failed")
            return
        self._writes += 1
        if self._writes % _PRUNE_EVERY == 0:
            self._prune()

    def tg_for_max(self, max_chat_id: Any, max_msg_id: Any) -> int | None:
        """First Telegram message that mirrors a Max message, if known."""
        row = self._db.execute(
            "SELECT tg_msg_id FROM msgmap WHERE max_chat_id=? AND max_msg_id=? "
            "ORDER BY rowid LIMIT 1",
            (str(max_chat_id), str(max_msg_id)),
        ).fetchone()
        return row[0] if row else None

    def text_for_max(self, max_chat_id: Any, max_msg_id: Any) -> str | None:
        row = self._db.execute(
            "SELECT text FROM msgmap WHERE max_chat_id=? AND max_msg_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (str(max_chat_id), str(max_msg_id)),
        ).fetchone()
        return row[0] if row else None

    def set_text(self, max_chat_id: Any, max_msg_id: Any, text: str) -> None:
        self._db.execute(
            "UPDATE msgmap SET text=? WHERE max_chat_id=? AND max_msg_id=?",
            (text, str(max_chat_id), str(max_msg_id)),
        )
        self._db.commit()

    def max_for_tg(self, tg_msg_id: int) -> tuple[Any, str] | None:
        """``(max_chat_id, max_msg_id)`` for a Telegram message, if known."""
        row = self._db.execute(
            "SELECT max_chat_id, max_msg_id FROM msgmap WHERE tg_msg_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (int(tg_msg_id),),
        ).fetchone()
        if not row:
            return None
        chat, msg = row
        try:
            chat = int(chat)
        except ValueError:
            pass
        return chat, msg

    def _prune(self) -> None:
        try:
            self._db.execute(
                "DELETE FROM msgmap WHERE rowid <= "
                "(SELECT MAX(rowid) FROM msgmap) - ?",
                (MAX_ROWS,),
            )
            self._db.commit()
        except Exception:
            log.exception("MessageMap prune failed")

    def close(self) -> None:
        self._db.close()
