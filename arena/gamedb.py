"""The arena's games in SQLite (data_dir/games.db), in place of one JSON file a game.

The engine is the only writer; the website reads the same file (WAL mode, so a reader never
waits for the writer). Each game is stored whole, as the JSON it always was, plus a few
indexed columns for lookups. Every insert or edit stamps the row with the next `version`, so
a reader catches up with `since(version)`: new games and games edited in place (a re-label, a
pruned replay, a map stamp) alike, without reading everything again.

On first open, an empty database imports data_dir/games/*.json in one transaction. The JSON
files are left as they are (a backup); nothing writes them any more.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger("arena")

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id      INTEGER PRIMARY KEY,
    version INTEGER NOT NULL,
    a       TEXT, b TEXT, suite TEXT, map TEXT, lane TEXT, played TEXT,
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS games_version ON games(version);
CREATE INDEX IF NOT EXISTS games_a ON games(a);
CREATE INDEX IF NOT EXISTS games_b ON games(b);
CREATE INDEX IF NOT EXISTS games_map ON games(suite, map);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _row(d: dict, version: int) -> tuple:
    return (int(d["id"]), version, d.get("a"), d.get("b"), d.get("suite"), d.get("map"),
            d.get("lane"), d.get("played"), json.dumps(d, separators=(",", ":")))


class GameDB:
    def __init__(self, path: Path, json_dir: Path | None = None, readonly: bool = False):
        self.path, self.readonly = Path(path), readonly
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=60,
                                  isolation_level=None)          # autocommit; explicit BEGIN below
        self.db.execute("PRAGMA busy_timeout = 60000")
        if not readonly:
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = NORMAL")
            self.db.executescript(SCHEMA)
            if json_dir is not None:
                self._import(Path(json_dir))
        self._version = self._max_version()

    # ------------------------------------------------------------------ writing (the engine)
    def _max_version(self) -> int:
        try:
            r = self.db.execute("SELECT MAX(version) FROM games").fetchone()
        except sqlite3.OperationalError:              # a reader before the engine made the table
            return 0
        return int(r[0] or 0)

    def _import(self, json_dir: Path):
        """The JSON files of a store that hasn't moved yet, once (the database is empty)."""
        if self.db.execute("SELECT value FROM meta WHERE key = 'imported'").fetchone():
            return
        files = sorted(json_dir.glob("*.json")) if json_dir.is_dir() else []
        rows, bad = [], 0
        for p in files:
            try:
                rows.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                bad += 1
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0:
                    self.db.executemany("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)",
                                        (_row(d, 1) for d in rows))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('imported', ?)",
                                (f"{len(rows)} games from {json_dir}",))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        if rows or bad:
            log.info("games: imported %d games from %s into %s (%d unreadable files skipped)",
                     len(rows), json_dir, self.path.name, bad)

    def put(self, d: dict):
        """Inserts or replaces one game (a dict with its id), stamped with the next version."""
        if self.readonly:
            raise RuntimeError("a read-only game store")
        with self.lock:
            self._version += 1
            self.db.execute("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?)", _row(d, self._version))

    def put_many(self, ds):
        if self.readonly:
            raise RuntimeError("a read-only game store")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for d in ds:
                    self._version += 1
                    self.db.execute("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?)",
                                    _row(d, self._version))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def delete(self, ids):
        """Removes games (a migration's removed submissions). Readers see it on a full load."""
        if self.readonly:
            raise RuntimeError("a read-only game store")
        with self.lock:
            self.db.executemany("DELETE FROM games WHERE id = ?", ((int(i),) for i in ids))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('epoch', ?)", (str(self._epoch() + 1),))

    def _epoch(self) -> int:
        try:
            r = self.db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(r[0]) if r else 0

    def epoch(self) -> int:
        """Bumped when games are deleted: a reader that sees it change loads everything again."""
        with self.lock:
            return self._epoch()

    # ------------------------------------------------------------------ reading
    def all(self) -> list[dict]:
        with self.lock:
            try:
                rows = self.db.execute("SELECT version, data FROM games ORDER BY id").fetchall()
            except sqlite3.OperationalError:
                return []
        self._version = max([self._version] + [r[0] for r in rows])
        return [json.loads(r[1]) for r in rows]

    def since(self, version: int) -> tuple[list[dict], int]:
        """Games added or edited after `version`, in id order, and the version to ask from next."""
        with self.lock:
            try:
                rows = self.db.execute("SELECT version, data FROM games WHERE version > ? ORDER BY id",
                                       (int(version),)).fetchall()
            except sqlite3.OperationalError:          # the engine hasn't made the table yet
                return [], version
        if not rows:
            return [], version
        return [json.loads(r[1]) for r in rows], max(r[0] for r in rows)

    def get(self, gid: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT data FROM games WHERE id = ?", (int(gid),)).fetchone()
        return json.loads(r[0]) if r else None

    def count(self) -> int:
        with self.lock:
            return int(self.db.execute("SELECT COUNT(*) FROM games").fetchone()[0])

    @property
    def version(self) -> int:
        return self._version
