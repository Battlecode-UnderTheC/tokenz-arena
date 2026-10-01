"""The competition server, for the bridge and the /api/v1/server/* endpoints (DESIGN.md
section 8): the documented key API (https://game.battlecode.au/docs/api) plus the `bcweb`
package for what only the website has (quota, brackets with per-game maps).

Every read is cached (leaderboard/ratings 60 s, battles/quota 15 s, finished battles,
replays and past tournaments forever). On a 429, or when our own budget for the minute is
spent, the cached copy is returned marked stale; without one, RateLimited says when to retry.
Replay downloads follow the signed redirect without forwarding the key.

The key is a secret: it's read from a file, held in this object only, and never logged,
returned, or put in an exception message.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path

log = logging.getLogger("arena.server")

DEFAULT_URL = "https://game.battlecode.au"
DEFAULT_KEY_FILE = "~/.config/tokenz-arena/unswbc-key"
TOOLKIT_KEYS = "~/.unswbc/keys.json"         # `unswbc auth set` writes {server url: key}
KEY_SHAPE = re.compile(r"^bc_[A-Za-z0-9_-]+$")
UA = "tokenz-arena (UnderTheC team arena)"

# Cache lifetimes in seconds (None = forever).
TTL_LADDER = 60          # /leaderboard, /ratings
TTL_FAST = 15            # /battles, quota, /queue
TTL_SLOW = 300           # /maps, /teams, /tournaments
DONE = ("completed", "failed", "cancelled", "error")

# Our own pacing, under the server's limits (120/min per key, 30/min for the two ladders).
BUDGET = {"all": 100, "ladder": 25}


class ServerError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class RateLimited(ServerError):
    def __init__(self, retry_after: float):
        super().__init__(f"the competition server is rate limiting us; retry in {retry_after:.0f} s",
                         429)
        self.retry_after = retry_after


class NoKey(ServerError):
    def __init__(self):
        super().__init__("no key configured", 503)


# ---------------------------------------------------------------------------- the key
def load_key(key_file: str | None = None, url: str = DEFAULT_URL) -> str | None:
    """The member key from `key_file` (a bare key, JSON {"key": ...}, or the toolkit's
    {server url: key}); falls back to the toolkit's ~/.unswbc/keys.json. None if neither."""
    for path in dict.fromkeys((key_file or DEFAULT_KEY_FILE, TOOLKIT_KEYS)):
        try:
            text = Path(path).expanduser().read_text().strip()
        except OSError:
            continue
        key = text
        if text.startswith("{"):
            try:
                table = json.loads(text)
            except ValueError:
                continue
            key = ((table.get("key") or table.get(url.rstrip("/")) or table.get(DEFAULT_URL) or "")
                   if isinstance(table, dict) else "")
        key = str(key).strip()
        if KEY_SHAPE.match(key):
            return key
    return None


# ---------------------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()
    for name, (filename, blob, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def finished(d) -> bool:
    """A battle (GET /battles/:id) whose games have all finished."""
    if not isinstance(d, dict):
        return False
    m = d.get("match") or {}
    return m.get("status") in DONE and all(g.get("status") in DONE for g in d.get("games") or [])


class Client:
    """The key API with a cache. Thread-safe; one per arena."""

    def __init__(self, key: str | None, url: str = DEFAULT_URL, timeout: float = 30,
                 clock=time.time):
        self._key = key
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.clock = clock
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float | None, float, object]] = {}   # path -> (expires, at, data)
        self.sent: dict[str, deque] = {"all": deque(), "ladder": deque()}
        self.blocked_until: dict[str, float] = {"all": 0.0, "ladder": 0.0}
        self.requests = 0
        self.last_error: str | None = None

    def __repr__(self):
        return f"Client({self.url}, key={'set' if self._key else 'none'})"

    @property
    def configured(self) -> bool:
        return bool(self._key)

    # ------------------------------------------------------------------ pacing
    @staticmethod
    def _bucket(path: str) -> str:
        return "ladder" if path.lstrip("/").split("?")[0] in ("leaderboard", "ratings") else "all"

    def _admit(self, bucket: str) -> float:
        """0 if a request may go now (and counts it), else seconds until one may."""
        now = self.clock()
        buckets = {bucket, "all"}
        with self.lock:
            for b in buckets:
                if self.blocked_until[b] > now:
                    return self.blocked_until[b] - now
                q = self.sent[b]
                while q and now - q[0] >= 60:
                    q.popleft()
                if len(q) >= BUDGET[b]:
                    return 60 - (now - q[0])
            for b in buckets:
                self.sent[b].append(now)
            self.requests += 1
            return 0.0

    # ------------------------------------------------------------------ raw requests
    def _send(self, method: str, path: str, body: bytes | None = None,
              ctype: str | None = None) -> tuple[int, dict, bytes]:
        if not self._key:
            raise NoKey()
        wait = self._admit(self._bucket(path))
        if wait:
            raise RateLimited(wait)
        req = urllib.request.Request(f"{self.url}/api/v1/{path.lstrip('/')}", data=body, method=method)
        req.add_header("Authorization", f"Bearer {self._key}")
        req.add_header("User-Agent", UA)
        req.add_header("Origin", self.url)
        req.add_header("Accept", "application/json")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, dict(e.headers or {}), e.read() or b""
        except (urllib.error.URLError, OSError) as e:
            raise ServerError(f"cannot reach {self.url}: {getattr(e, 'reason', e)}") from None
        if status in (301, 302, 303, 307, 308):
            return status, headers, raw
        if status == 429:
            try:
                after = float(headers.get("Retry-After") or headers.get("retry-after") or 30)
            except ValueError:
                after = 30.0
            with self.lock:
                b = self._bucket(path)
                self.blocked_until[b] = max(self.blocked_until[b], self.clock() + after)
            raise RateLimited(after)
        try:
            msg = json.loads(raw).get("error")
        except (ValueError, AttributeError):
            msg = None
        raise ServerError(f"{method} /{path.lstrip('/').split('?')[0]}: {msg or f'HTTP {status}'}",
                          status)

    def _json(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None):
        status, _, raw = self._send(method, path, body, ctype)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise ServerError(f"{method} /{path}: the server replied with something that isn't JSON",
                              status) from None

    # ------------------------------------------------------------------ cached reads
    def get(self, path: str, ttl: float | None, keep=None) -> tuple[object, bool]:
        """(data, stale). `ttl` None caches forever; `keep(data)` True also caches forever
        (a battle once it's finished). Errors fall back to a cached copy when there is one."""
        now = self.clock()
        with self.lock:
            hit = self.cache.get(path)
        if hit and (hit[0] is None or hit[0] > now):
            return hit[2], False
        try:
            data = self._json("GET", path)
        except NoKey:
            raise
        except ServerError as e:
            self.last_error = str(e)
            if hit:
                return hit[2], True
            raise
        forever = ttl is None or (keep is not None and keep(data))
        with self.lock:
            self.cache[path] = (None if forever else now + ttl, now, data)
        return data, False

    def peek(self, path: str):
        """The cached copy, however old (None if never fetched)."""
        with self.lock:
            hit = self.cache.get(path)
        return hit[2] if hit else None

    def forget(self, prefix: str):
        with self.lock:
            for k in [k for k in self.cache if k.startswith(prefix)]:
                self.cache.pop(k, None)

    # ------------------------------------------------------------------ the API
    def me(self):
        return self.get("me", TTL_SLOW)

    def team(self):
        return self.get("team", TTL_LADDER)

    def submissions(self):
        return self.get("submissions", TTL_FAST)

    def battles(self, limit: int = 50):
        return self.get(f"battles?limit={max(1, min(int(limit), 200))}", TTL_FAST)

    def battle(self, bid: int):
        """A battle, or one game of it by its game id. Cached forever once finished."""
        return self.get(f"battles/{int(bid)}", TTL_FAST, keep=finished)

    def leaderboard(self):
        return self.get("leaderboard", TTL_LADDER)

    def ratings(self):
        return self.get("ratings", TTL_LADDER)

    def teams(self):
        return self.get("teams", TTL_SLOW)

    def team_profile(self, tid: int):
        return self.get(f"teams/{int(tid)}", TTL_LADDER)

    def tournaments(self):
        return self.get("tournaments", TTL_SLOW)

    def tournament(self, tid: int):
        return self.get(f"tournaments/{int(tid)}", TTL_LADDER,
                        keep=lambda d: isinstance(d, dict) and d.get("status") == "done")

    def maps(self):
        return self.get("maps", TTL_SLOW)

    def queue(self):
        return self.get("queue", TTL_FAST)

    def replay(self, game_id: int) -> bytes:
        """A finished game's replay file as served (gzip). The API answers with a redirect to
        a signed link, which is fetched without the Authorization header."""
        status, headers, raw = self._send("GET", f"battles/{int(game_id)}/replay")
        if status not in (301, 302, 303, 307, 308):
            return raw
        loc = headers.get("Location") or headers.get("location")
        if not loc:
            raise ServerError(f"replay {game_id}: redirect without a location", status)
        loc = urllib.parse.urljoin(f"{self.url}/api/v1/battles/{int(game_id)}/replay", loc)
        req = urllib.request.Request(loc, headers={"User-Agent": UA})      # no key on this hop
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 120)) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise ServerError(f"replay {game_id}: the download link answered HTTP {e.code}",
                              e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise ServerError(f"replay {game_id}: download failed ({getattr(e, 'reason', e)})") from None

    # ------------------------------------------------------------------ writes (outward-facing)
    def upload(self, name: str, language: str, description: str, zip_bytes: bytes,
               filename: str = "bot.zip") -> dict:
        body, ctype = _multipart({"name": name, "language": language, "description": description},
                                 {"zip": (filename, zip_bytes, "application/zip")})
        out = self._json("POST", "submissions", body, ctype)
        self.forget("submissions")
        return out

    def activate(self, sid: int) -> dict:
        out = self._json("POST", f"submissions/{int(sid)}/activate", b"", "application/json")
        self.forget("submissions")
        self.forget("team")
        return out

    def challenge(self, team_id: int, ranked: bool, map_ids: list[int] | None = None) -> dict:
        body: dict = {"teamId": int(team_id), "ranked": bool(ranked)}
        if map_ids and not ranked:
            body["mapIds"] = [int(m) for m in map_ids]
        out = self._json("POST", "battles", json.dumps(body).encode(), "application/json")
        self.forget("battles?")
        return out


# ---------------------------------------------------------------------------- the website (bcweb)
class Web:
    """What only the website has, through `bcweb` and the member's web login
    (~/.unswbc-web/credentials). Every call is optional: a failure returns None (or the
    last good copy) with the reason in `last_error`."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float | None, object]] = {}
        self.last_error: str | None = None

    def _cached(self, key: str, ttl: float | None, fn):
        now = self.clock()
        with self.lock:
            hit = self.cache.get(key)
        if hit and (hit[0] is None or hit[0] > now):
            return hit[1]
        try:
            value = fn()
        except BaseException as e:      # bcweb raises BcError, urllib errors, SystemExit on no login
            if isinstance(e, KeyboardInterrupt):
                raise
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            return hit[1] if hit else None
        with self.lock:
            self.cache[key] = (None if ttl is None else now + ttl, value)
        return value

    def availability(self, team_id: int) -> dict | None:
        """{dev, remaining, freeIn} for challenges against team_id."""
        def fn():
            from bcweb import core
            return core.availability(int(team_id))
        return self._cached(f"availability:{team_id}", TTL_FAST, fn)

    def bracket(self, tid, done: bool, names: tuple = ()) -> dict | None:
        """The site's bracket for a tournament, with each game's map and winner slot. The site
        names tournaments by slug ('sprint') where the API uses numbers, so `names` (the API's
        name and kind) are matched too."""
        want = {str(tid).lower(), *(str(n).lower() for n in names if n)}

        def fn():
            from bcweb import core
            return next((t for t in core._tournaments()
                         if {str(t.get(k)).lower() for k in ("id", "name", "kind")} & want), None)
        return self._cached(f"bracket:{tid}", None if done else TTL_LADDER, fn)
