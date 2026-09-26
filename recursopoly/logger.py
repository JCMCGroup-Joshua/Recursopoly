"""Recursopoly score logger.

Appends rows to scores.csv. The file is created with a header row on first
write and is never overwritten. A process-wide lock keeps rows from several
games (handled on different threads) from interleaving.

Phase 5's leaderboard and history pages read this same file back.
"""

import csv
import logging
import os
import threading
from datetime import datetime, timezone

log = logging.getLogger("recursopoly.logger")

COLUMNS = [
    "timestamp",
    "join_code",
    "event_type",
    "player_name",
    "board_id",
    "position",
    "money",
    "details",
]


def _blank(value):
    return "" if value is None else value


class ScoreLogger:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def log_row(self, join_code, event_type, player_name="", board_id=None,
                position=None, money=None, details=""):
        row = [
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            join_code,
            event_type,
            player_name,
            _blank(board_id),
            _blank(position),
            _blank(money),
            details,
        ]
        self._write([row])

    def log_events(self, join_code, events):
        """Write a batch of game_engine.GameEvent objects in one locked write."""
        if not events:
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [
            [stamp, join_code, e.event_type, e.player_name, _blank(e.board_id),
             _blank(e.position), _blank(e.money), e.details]
            for e in events
        ]
        self._write(rows)

    def _write(self, rows):
        with self._lock:
            try:
                folder = os.path.dirname(self.path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                new_file = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
                # Append mode only: scores.csv is never truncated.
                with open(self.path, "a", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    if new_file:
                        writer.writerow(COLUMNS)
                    writer.writerows(rows)
            except OSError:
                # Logging must never crash a game in progress.
                log.exception("Recursopoly could not write to %s", self.path)

    def read_rows(self):
        """Return all rows as dicts (for Phase 5 stats pages)."""
        with self._lock:
            if not os.path.exists(self.path):
                return []
            with open(self.path, newline="", encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
