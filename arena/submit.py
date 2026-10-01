"""Accepting a submission: validate the zip, store it, unpack it, run the checks.

Checks (DESIGN.md section 1): a safe zip (size, no absolute paths, no "..", no links),
bot.toml at the root or one folder down with language c/cpp/python, no environment-variable
reads, then one sandboxed game against the reference bot (or itself) on the smallest map.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import stat
import tomllib
import zipfile
from pathlib import Path

from arena import runner
from arena.state import CHECKING, ENGINE, PLACING, QUEUED, REJECTED, RETIRED, Arena, Submission, now

MAX_ZIP = 4 * 1024 * 1024           # the competition server's limit
MAX_UNPACKED = 32 * 1024 * 1024
# What the toolkit accepts in bot.toml (unswbc/project.py), normalised.
LANGUAGES = {"py": "python", "python": "python", "cpp": "cpp", "c++": "cpp", "cxx": "cpp", "c": "c"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
_ENV = {
    "python": re.compile(r"\bos\.(environ|getenv|putenv)\b|\bgetenv\b"),
    "c": re.compile(r"\b(getenv|secure_getenv|environ)\b"),
    "cpp": re.compile(r"\b(getenv|secure_getenv|environ)\b"),
}
_SOURCE_EXT = {".py", ".c", ".h", ".cc", ".cpp", ".hpp", ".hh", ".cxx"}


class Rejected(ValueError):
    pass


def bot_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise Rejected("bot name: 1-48 chars of a-z, 0-9, '-', '_', '.', starting with a letter or digit")
    if name.startswith("server:"):
        raise Rejected("bot names starting with 'server:' are reserved")
    return name


def _validate_zip(blob: bytes) -> zipfile.ZipFile:
    if len(blob) > MAX_ZIP:
        raise Rejected(f"zip is {len(blob) / 2**20:.1f} MB; the limit is {MAX_ZIP // 2**20} MB")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise Rejected("not a zip file")
    total = 0
    for info in z.infolist():
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
            raise Rejected(f"unsafe path in zip: {name}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise Rejected(f"links aren't allowed in the zip: {name}")
        total += info.file_size
        if total > MAX_UNPACKED:
            raise Rejected("zip unpacks to more than 32 MB")
    names = [i.filename for i in z.infolist()]
    if not any(n == "bot.toml" or (n.count("/") == 1 and n.endswith("/bot.toml")) for n in names):
        raise Rejected("no bot.toml at the zip's root or one folder down")
    return z


def _unpack(blob: bytes, dest: Path) -> Path:
    """Unpacks safely into dest; returns the project root (where bot.toml is)."""
    z = _validate_zip(blob)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    z.extractall(dest)
    for junk in list(dest.rglob("__MACOSX")) + list(dest.rglob(".DS_Store")):
        shutil.rmtree(junk, ignore_errors=True) if junk.is_dir() else junk.unlink()
    if (dest / "bot.toml").exists():
        return dest
    inner = [p for p in dest.iterdir() if p.is_dir() and (p / "bot.toml").exists()]
    if len(inner) == 1:
        # Lift the project to the top, so the directory name stays the submission id.
        tmp = dest.with_name(dest.name + ".lift")
        shutil.move(str(inner[0]), tmp)
        shutil.rmtree(dest)
        tmp.rename(dest)
        return dest
    raise Rejected("no bot.toml at the zip's root or one folder down")


def _language(root: Path) -> str:
    try:
        toml = tomllib.loads((root / "bot.toml").read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise Rejected(f"bot.toml doesn't parse: {e}")
    lang = str(toml.get("project", {}).get("language", "")).lower()
    if lang not in LANGUAGES:
        raise Rejected(f"bot.toml [project] language must be one of {', '.join(LANGUAGES)} (got {lang!r})")
    return LANGUAGES[lang]


def _env_reads(root: Path, lang: str) -> str | None:
    pattern = _ENV[lang]
    for p in sorted(root.rglob("*")):
        if p.suffix in _SOURCE_EXT and p.is_file():
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if pattern.search(line):
                    return f"{p.relative_to(root)}:{n}: {line.strip()[:120]}"
    return None


def _map_names(cfg, root: Path) -> list[str]:
    """Warnings for source that names a map in a string literal: possible map-keyed logic,
    which overfits our maps and breaks on new tournament maps."""
    names = set()
    for suite in ("official", "tournament", "server"):
        d = cfg.map_dir(suite)
        for p in d.glob("*.map") if d and d.is_dir() else []:
            names.add(p.stem.lower())
            for line in p.read_text(errors="replace").splitlines()[:5]:
                if line.startswith("MAP_NAME"):
                    names.add(line.split(None, 1)[1].strip().lower())
    names = {n for n in names if len(n) >= 4 and n not in {"default", "arena", "maze"}}
    if not names:
        return []
    pat = re.compile(r"""["']([^"'\n]{4,40})["']""")
    out = []
    for p in sorted(root.rglob("*")):
        if p.suffix not in _SOURCE_EXT or not p.is_file():
            continue
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            for lit in pat.findall(line):
                if lit.strip().lower() in names:
                    out.append(f"{p.relative_to(root)}:{n}: names the map {lit!r} "
                               "(map-keyed logic overfits; new tournament maps break it)")
        if len(out) >= 5:
            break
    return out[:5]


def accept(arena: Arena, *, blob: bytes, bot: str, owner: str, note: str = "",
           parent: str | None = None, source: str | None = None, lane: str = "draft",
           agent: str | None = None) -> tuple[Submission, bool]:
    """Stores a new submission (status 'checking'); returns (submission, is_new). A zip whose
    content was already submitted returns the original. Raises Rejected for bad input."""
    bot = bot_name(bot)
    if lane not in ("draft", "ranked"):
        raise Rejected("lane must be 'draft' or 'ranked'")
    _validate_zip(blob)
    sha = hashlib.sha256(blob).hexdigest()
    with arena.lock:
        if dup := arena.by_sha(sha):
            return dup, False
        if parent and parent not in arena.subs:
            raise Rejected(f"unknown parent {parent!r}")
        b = arena.bots.get(bot)
        if b is None:
            arena.bots[bot] = {"owner": owner, "active": True, "current": None}
            arena.save_bots()
        sid = arena.next_id(bot)
        s = Submission(id=sid, bot=bot, owner=owner, lane=lane, status=CHECKING, sha256=sha,
                       agent=agent, parent=parent, note=note or "", source=source, engine=ENGINE)
        arena.subs[sid] = s
        (arena.cfg.submissions / sid).mkdir(parents=True, exist_ok=True)
        (arena.cfg.submissions / sid / "source.zip").write_bytes(blob)
        arena.save_sub(s)
        return s, True


def reject(arena: Arena, s: Submission, reason: str, log: str | None = None):
    with arena.lock:
        s.status, s.reason, s.check_log = REJECTED, reason, log
        arena.save_sub(s)


def check(arena: Arena, s: Submission) -> bool:
    """Unpacks and checks a submission; on success it's queued for games. Slow (one game)."""
    cfg = arena.cfg
    try:
        root = _unpack((cfg.submissions / s.id / "source.zip").read_bytes(), cfg.bots / s.id)
        s.language = _language(root)
        if hit := _env_reads(root, s.language):
            raise Rejected(f"reads environment variables (not allowed by the competition): {hit}")
        s.warnings = _map_names(cfg, root)
    except Rejected as e:
        reject(arena, s, str(e))
        return False
    maps = sorted(cfg.map_dir("official").glob("*.map"), key=lambda p: p.stat().st_size)
    opponent = _reference_dir(arena) or cfg.bots / s.id
    try:
        r = runner.play(maps[0], cfg.bots / s.id, opponent, cfg.seeds("official")[0],
                        cfg.replays / "checks", work_root=cfg.work,
                        timeout=cfg.get("play", "game_timeout", 1800))
    except runner.JudgeError as e:
        reject(arena, s, "didn't build or play: " + str(e).splitlines()[0], e.log[-4000:])
        return False
    if r.replay:
        r.replay.unlink(missing_ok=True)
    with arena.lock:
        s.check_log = f"check game on {maps[0].stem}: {r.winner or 'draw'} after {r.rounds} rounds ({r.reason})"
        if s.lane == "ranked":
            make_current(arena, s)
        else:
            s.status = QUEUED
        arena.save_sub(s)
    return True


def _reference_dir(arena: Arena) -> Path | None:
    ref = arena.cfg.get("play", "reference_bot")
    b = arena.bots.get(ref or "")
    if b and b.get("current") and (arena.cfg.bots / b["current"]).exists():
        return arena.cfg.bots / b["current"]
    return None


def retire(arena: Arena, s: Submission, by: str):
    """Takes a submission off the leaderboard: retired, and no longer its bot's current
    version. Its games, verdicts and source stay."""
    with arena.lock:
        b = arena.bots.get(s.bot)
        if b and b.get("current") == s.id:
            b["current"] = None
            arena.save_bots()
        s.status, s.retired_at, s.retired_by = RETIRED, now(), by
        arena.save_sub(s)


def make_current(arena: Arena, s: Submission, by: str | None = None):
    """Ranked: s becomes its bot's current submission and starts placement."""
    with arena.lock:
        b = arena.bots.setdefault(s.bot, {"owner": s.owner, "active": True, "current": None})
        old = arena.subs.get(b.get("current") or "")
        if old and old.id != s.id:
            old.status = RETIRED
            arena.save_sub(old)
        b["current"] = s.id
        arena.save_bots()
        s.lane, s.status, s.opponents, s.quick_opponents = "ranked", PLACING, None, None
        if by:
            s.promoted, s.promoted_by = now(), by
        arena.save_sub(s)
