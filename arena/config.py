"""Loads arena.toml (see arena.example.toml). Every setting has a default, so an empty or
missing file runs a local arena under ./data."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict = field(default_factory=dict)
    data_dir: Path = ROOT / "data"

    def get(self, section: str, key: str, default=None):
        return self.raw.get(section, {}).get(key, default)

    def path(self, value: str) -> Path:
        """A configured path: <repo> means this checkout, relative paths are under data_dir."""
        if value.startswith("<repo>/"):
            return ROOT / value.removeprefix("<repo>/")
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.data_dir / p

    @property
    def replays(self) -> Path:
        return self.data_dir / "replays"

    @property
    def work(self) -> Path:
        return self.data_dir / "work"

    @property
    def submissions(self) -> Path:
        return self.data_dir / "submissions"

    @property
    def games(self) -> Path:
        return self.data_dir / "games"

    @property
    def bots(self) -> Path:
        """Unpacked submissions, one directory each, named by submission id (the judge's
        build cache is keyed by directory name and content, so a bot compiles once)."""
        return self.data_dir / "bots"

    def map_dir(self, suite: str) -> Path | None:
        default = {"official": "<repo>/maps/official", "rotating": "maps/rotating",
                   "holdout": "maps/holdout", "server": "maps/server"}.get(suite)
        value = self.get("maps", suite, default)
        return self.path(value) if value else None

    def seeds(self, suite: str) -> list[int]:
        return list(self.get("seeds", suite, [1592614637]))


def load(path: Path | None = None) -> Config:
    path = path or ROOT / "arena.toml"
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    data = raw.get("data_dir")
    return Config(raw=raw, data_dir=Path(data).expanduser() if data else ROOT / "data")
