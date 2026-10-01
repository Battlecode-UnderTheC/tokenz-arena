"""Loads arena.toml (see arena.example.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict
    data_dir: Path

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
    def sources(self) -> Path:
        return self.data_dir / "sources"


def load(path: Path = ROOT / "arena.toml") -> Config:
    raw = tomllib.loads(path.read_text())
    return Config(raw=raw, data_dir=Path(raw.get("data_dir", "~/tokenz-arena-data")).expanduser())
