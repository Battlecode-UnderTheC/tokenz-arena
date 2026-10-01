#!/usr/bin/env python3
"""bcweb: a small read-mostly client for the competition website (https://game.battlecode.au).

Auth is the team member's web login (email + password) read at runtime from
~/.unswbc-web/credentials (line 1 email, line 2 password). The resulting session cookie is
cached in ~/.unswbc-web/session.json (mode 0600). Neither is ever printed.

The site is a SvelteKit app. Pages expose their data as `<path>/__data.json` (devalue-encoded),
which is what this tool reads; replays come from `/api/matches/<game-id>/replay`.
The documented JSON API under /api/v1 needs a per-member API key and is NOT used here.

Commands: login, whoami, submissions, leaderboard, battles, fetch, wait, challenge, quota.
Run `bcweb.py <command> -h` for options. Standard library only (Python 3.11).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://game.battlecode.au"
HOME = os.path.expanduser("~/.unswbc-web")
CRED_FILE = os.path.join(HOME, "credentials")
SESSION_FILE = os.path.join(HOME, "session.json")
CHALLENGE_LOG = os.path.join(HOME, "challenges.jsonl")
UA = "bcweb/1.0 (UnderTheC team tooling)"

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_league():
    # The league checkout (syeyoung-tokens.league) sits beside the main checkout; worktrees
    # live one level deeper, so walk up until a sibling of that name appears.
    d = HERE
    while True:
        cand = os.path.join(d, "syeyoung-tokens.league")
        if os.path.isdir(cand):
            return cand
        up = os.path.dirname(d)
        if up == d:
            return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
                                "syeyoung-tokens.league")
        d = up


LEAGUE_ROOT = _find_league()
REPLAY_ROOT = os.path.join(LEAGUE_ROOT, "replays")
DECODE_DIRS = [os.path.join(REPLAY_ROOT, "league-2", "analysis")]

MIN_GAP = 1.0      # seconds between any two requests
WAIT_MIN = 60      # minimum poll interval for `wait`


class BcError(Exception):
    pass


# --------------------------------------------------------------------------- http

_last_req = [0.0]


def _pace():
    gap = time.monotonic() - _last_req[0]
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last_req[0] = time.monotonic()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener_noredir = urllib.request.build_opener(_NoRedirect)


def _request(method, path, *, body=None, headers=None, cookie=None, follow=True, tries=3):
    """One HTTP request with pacing and backoff on 429/5xx/network errors.
    Returns (status, headers, bytes). Never includes credentials in raised messages."""
    url = path if path.startswith("http") else BASE + path
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    if cookie:
        h["Cookie"] = cookie
    delay = 5.0
    for attempt in range(tries):
        _pace()
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            opener = urllib.request.build_opener() if follow else _opener_noredir
            with opener.open(req, timeout=60) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            data = e.read() if e.fp else b""
            if e.code in (301, 302, 303, 307, 308) and not follow:
                return e.code, e.headers, data
            if e.code == 429 or e.code >= 500:
                if attempt + 1 < tries:
                    ra = e.headers.get("Retry-After")
                    wait = float(ra) if ra and ra.isdigit() else delay
                    print(f"[bcweb] HTTP {e.code} on {urllib.parse.urlsplit(url).path}; "
                          f"backing off {wait:.0f}s", file=sys.stderr)
                    time.sleep(min(wait, 300))
                    delay *= 2
                    continue
            return e.code, e.headers, data
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt + 1 < tries:
                print(f"[bcweb] network error ({type(e).__name__}); retrying in {delay:.0f}s",
                      file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                continue
            raise BcError(f"network error talking to {BASE}: {type(e).__name__}")
    raise BcError("unreachable")


# --------------------------------------------------------------------------- session

def _write_private(path, text):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _load_session():
    try:
        with open(SESSION_FILE) as f:
            return json.load(f).get("cookie")
    except (OSError, ValueError):
        return None


def login(quiet=False):
    try:
        with open(CRED_FILE) as f:
            lines = f.read().splitlines()
    except OSError:
        raise BcError(f"cannot read {CRED_FILE} (line 1 email, line 2 password)")
    if len(lines) < 2 or not lines[0].strip() or not lines[1]:
        raise BcError(f"{CRED_FILE} must have the email on line 1 and the password on line 2")
    body = json.dumps({"email": lines[0].strip(), "password": lines[1]}).encode()
    del lines
    st, hdrs, data = _request("POST", "/api/auth/sign-in/email", body=body, tries=1,
                              headers={"Content-Type": "application/json", "Origin": BASE})
    del body
    if st != 200:
        try:
            msg = json.loads(data).get("message") or json.loads(data).get("error")
        except Exception:
            msg = None
        raise BcError(f"login failed: HTTP {st}" + (f" ({msg})" if msg else ""))
    cookies = [c.split(";", 1)[0] for c in (hdrs.get_all("Set-Cookie") or [])]
    cookies = [c for c in cookies if "session" in c.split("=", 1)[0]]
    if not cookies:
        raise BcError("login returned no session cookie")
    cookie = "; ".join(cookies)
    _write_private(SESSION_FILE, json.dumps({"cookie": cookie, "at": _now_iso()}))
    if not quiet:
        print("logged in; session cached in ~/.unswbc-web/session.json (0600)")
    return cookie


def _cookie():
    return _load_session() or login(quiet=True)


def page_data(path, params=None):
    """GET <path>/__data.json, re-logging in once if the session has lapsed.
    Returns the merged dict of every page node's data."""
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = path.rstrip("/") + "/__data.json" + q
    for attempt in range(2):
        st, _, data = _request("GET", url, cookie=_cookie(),
                               headers={"Accept": "application/json"})
        if st in (401, 403):
            if attempt == 0:
                login(quiet=True)
                continue
            raise BcError(f"not authorised for {path} (HTTP {st})")
        if st != 200:
            raise BcError(f"GET {path} failed: HTTP {st}")
        nodes = _devalue_stream(data.decode())
        for n in nodes:
            if isinstance(n, dict) and n.get("type") == "redirect":
                loc = n.get("location", "")
                if loc.startswith("/login") and attempt == 0:
                    login(quiet=True)
                    break
                raise _Redirect(loc)
        else:
            merged = {}
            for n in nodes:
                if isinstance(n, dict) and n.get("type") not in ("redirect", "error", "skip"):
                    merged.update(n)
            return merged
    raise BcError(f"session could not be established for {path}")


class _Redirect(BcError):
    def __init__(self, loc):
        super().__init__(f"redirected to {loc}")
        self.location = loc


# --------------------------------------------------------------------------- devalue

def _unflatten(values):
    if isinstance(values, int):
        return None
    memo = {}

    def h(i):
        if not isinstance(i, int) or i < 0:
            return {-3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: -0.0}.get(i)
        if i in memo:
            return memo[i]
        v = values[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):
                t = v[0]
                if t == "Date":
                    r = v[1]
                elif t == "Set":
                    r = [h(x) for x in v[1:]]
                elif t == "Map":
                    r = {str(h(v[k])): h(v[k + 1]) for k in range(1, len(v), 2)}
                elif t == "BigInt":
                    r = int(v[1])
                else:
                    r = v[1:]
                memo[i] = r
                return r
            r = []
            memo[i] = r
            r.extend(h(x) for x in v)
            return r
        if isinstance(v, dict):
            r = {}
            memo[i] = r
            for k, x in v.items():
                r[k] = h(x)
            return r
        memo[i] = v
        return v

    return h(0)


def _devalue_stream(text):
    """Decode a SvelteKit __data.json response (one JSON object per line; later lines are
    streamed promise chunks, which are ignored)."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("type") == "data":
            for n in d.get("nodes") or []:
                if n and n.get("type") == "data":
                    out.append(_unflatten(n["data"]))
        elif d.get("type") in ("redirect", "error"):
            out.append(d)
    return out


# --------------------------------------------------------------------------- helpers

def _now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_time(s):
    if not s:
        return "-"
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t.astimezone(dt.timezone.utc).strftime("%m-%d %H:%M")
    except ValueError:
        return s[:16]


def _battle_id(arg):
    s = str(arg).strip()
    if s[:1] in "Mm":
        s = s[1:]
    if not s.isdigit():
        raise BcError(f"not a battle id: {arg}")
    return int(s)


def _table(rows, headers):
    w = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            w[i] = max(w[i], len(str(c)))
    fmt = "  ".join("{:<%d}" % x for x in w)
    print(fmt.format(*headers).rstrip())
    for r in rows:
        print(fmt.format(*[str(c) for c in r]).rstrip())


def _load_decode():
    for d in DECODE_DIRS:
        if os.path.exists(os.path.join(d, "decode.py")):
            sys.path.insert(0, d)
            try:
                import decode  # type: ignore
                return decode
            except Exception:
                return None
            finally:
                sys.path.pop(0)
    return None


# --------------------------------------------------------------------------- commands

def cmd_login(a):
    login()
    d = page_data("/team")
    t = d.get("team") or {}
    print(f"team: {t.get('name')} (id {t.get('id')}), elo {t.get('elo')}")


def cmd_whoami(a):
    d = page_data("/team")
    t = d.get("team") or {}
    u = d.get("user") or {}
    print(f"user: {u.get('username')}  team: {t.get('name')} (id {t.get('id')})  "
          f"elo {t.get('elo')}  settings {t.get('settings')}")


def _submissions():
    return page_data("/submissions").get("submissions") or []


def cmd_submissions(a):
    subs = _submissions()
    rows = []
    for s in subs[: a.limit]:
        w, d_, l = s.get("wins") or 0, s.get("draws") or 0, s.get("losses") or 0
        rows.append([("*" if s.get("status") == "active" else " ") + str(s["id"]),
                     s.get("name"), _short_time(s.get("uploadedAt")), s.get("status"),
                     f"{w}-{d_}-{l}", s.get("language")])
    _table(rows, ["id", "name", "uploaded(UTC)", "status", "W-D-L", "lang"])
    print("(* = live: status 'active')")
    if a.maps:
        for s in subs[: a.limit]:
            bm = s.get("byMap") or {}
            if not bm:
                continue
            print(f"\n{s['id']} {s.get('name')} per map:")
            for m, r in sorted(bm.items()):
                n = r["wins"] + r["draws"] + r["losses"]
                pct = 100.0 * r["wins"] / n if n else 0
                print(f"  {m:<20} {r['wins']:>3}-{r['draws']}-{r['losses']:<3} {pct:5.1f}%")


def cmd_leaderboard(a):
    teams, mine, total = [], None, None
    pages = max(1, a.pages)
    for p in range(1, pages + 1):
        d = page_data("/leaderboard", {"page": p} if p > 1 else None)
        mine = mine or d.get("mine")
        total = d.get("total")
        teams += d.get("teams") or []
        if p >= (d.get("pages") or 1):
            break
    myid = (mine or {}).get("id")
    rows = [[("*" if t["id"] == myid else " ") + str(t["rank"]), t["name"], t["elo"],
             t.get("wins"), t["id"]] for t in teams]
    if mine and myid not in {t["id"] for t in teams}:
        rows.append(["...", "", "", "", ""])
        rows.append(["*" + str(mine["rank"]), mine["name"], mine["elo"], mine.get("wins"), myid])
    _table(rows, ["rank", "team", "rating", "wins", "team-id"])
    print(f"({total} teams; * = us. 10 per page; --pages N for more)")


def _my_battles(limit):
    out, page, myid = [], 1, None
    while len(out) < limit:
        d = page_data("/battles", {"mine": 1, "page": page} if page > 1 else {"mine": 1})
        myid = d.get("myTeamId")
        bs = d.get("battles") or []
        out += bs
        if not bs or len(out) >= (d.get("total") or 0):
            break
        page += 1
    return out[:limit], myid


def _perspective(b, myid):
    us_a = b["a"]["id"] == myid
    us, them = (b["a"], b["b"]) if us_a else (b["b"], b["a"])
    res = b.get("resultsA") or []
    if not us_a:
        res = [{"win": "loss", "loss": "win"}.get(r, r) for r in res]
    w, l = (b.get("winsA"), b.get("winsB")) if us_a else (b.get("winsB"), b.get("winsA"))
    change = b.get("eloChangeA") if us_a else b.get("eloChangeB")
    oc = b.get("outcome")
    outcome = ("win" if (oc == "a") == us_a else "loss") if oc in ("a", "b") else (oc or "pending")
    return us, them, res, w, l, change, outcome, "A" if us_a else "B"


def cmd_battles(a):
    bs, myid = _my_battles(a.limit)
    rows = []
    for b in bs:
        us, them, res, w, l, ch, oc, side = _perspective(b, myid)
        kind = ("ranked" if b.get("ranked") else "unranked") + \
               ("/scrim" if b.get("challenge") else "/auto")
        extra = ""
        wt = b.get("wait")
        if isinstance(wt, dict):
            extra = f"queued: {wt.get('ahead')} ahead, ~{round((wt.get('ms') or 0) / 1000)}s"
        elif wt is not None:
            extra = f"queued: {wt}"
        rows.append([f"M{b['id']}", _short_time(b.get("at")), kind,
                     f"{them['name']} ({them.get('elo')})", side,
                     f"{w}-{l}" if w is not None else "-", oc,
                     "" if ch is None else f"{ch:+d}",
                     "".join({"win": "W", "loss": "L", "draw": "D"}.get(r, ".") for r in res),
                     extra])
    _table(rows, ["battle", "at(UTC)", "kind", "opponent (elo)", "side", "W-L", "result",
                  "elo", "games", ""])
    print("kind: ranked/unranked; scrim = a team requested it, auto = autoscrim. "
          "side: our slot (A/B) on the site. games: our per-game results, '.' = not played yet.")
    if a.maps:
        print()
        for b in bs:
            info = _battle_info(b["id"])
            games = info.get("games") or []
            print(f"M{b['id']}: " + ", ".join(
                f"{g.get('mapName')}={_game_result(g, info, myid)}" for g in games))


def _battle_info(bid):
    d = page_data("/visualiser", {"match": bid})
    if not d.get("battle"):
        raise BcError(f"no battle M{bid} visible to us")
    return d


def _game_result(g, info, myid):
    m = info["battle"]["match"]
    if g.get("status") != "completed":
        return g.get("status") or "?"
    w = g.get("winner")
    if w not in ("a", "b"):
        return w or "draw"
    # Team A/B are fixed per battle on the site; per-game side swaps are handled in fetch.
    us = "a" if m.get("teamAId") == myid else "b"
    return "W" if w == us else "L"


def _download_game(gid):
    st, hdrs, data = _request("GET", f"/api/matches/{gid}/replay", cookie=_cookie())
    if st == 409:
        return None  # unfinished
    if st in (401, 403):
        login(quiet=True)
        st, hdrs, data = _request("GET", f"/api/matches/{gid}/replay", cookie=_cookie())
    if st != 200:
        raise BcError(f"replay for game {gid}: HTTP {st}")
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def fetch(bid, out_root=None, force=False):
    info = _battle_info(bid)
    me = page_data("/battles", {"mine": 1})  # cheap way to learn our team id
    myid = me.get("myTeamId")
    subs = {s["id"]: s.get("name") for s in _submissions()}
    m = info["battle"]["match"]
    games = info.get("games") or []
    out_dir = os.path.join(out_root or REPLAY_ROOT, f"M{bid}")
    os.makedirs(out_dir, exist_ok=True)
    decode = _load_decode()
    teamA, teamB = info["battle"].get("teamAName"), info["battle"].get("teamBName")
    opp = teamB if m.get("teamAId") == myid else teamA
    print(f"M{bid}: {teamA} vs {teamB}  {'ranked' if m.get('ranked') else 'unranked'}  "
          f"status {m.get('status')}  -> {out_dir}")
    meta_games = []
    pending = 0
    for g in games:
        gid = g["id"]
        path = os.path.join(out_dir, f"M{gid}.replay")
        if g.get("status") != "completed" or not g.get("hasReplay", True):
            pending += 1
            print(f"  M{gid} {g.get('mapName')}: {g.get('status')} (no replay yet)")
            continue
        if force or not os.path.exists(path):
            data = _download_game(gid)
            if data is None:
                pending += 1
                print(f"  M{gid} {g.get('mapName')}: not finished")
                continue
            with open(path, "wb") as f:
                f.write(data)
        line, rec = _summarise(path, g, decode, subs, opp)
        meta_games.append(rec)
        print("  " + line)
    with open(os.path.join(out_dir, "battle.json"), "w") as f:
        json.dump({"battle": bid, "fetchedAt": _now_iso(), "match": m,
                   "teamA": teamA, "teamB": teamB, "ourTeamId": myid,
                   "games": meta_games}, f, indent=1, default=str)
    return pending


def _summarise(path, g, decode, subs, opp):
    rec = {"id": g["id"], "map": g.get("mapName"), "siteWinner": g.get("winner")}
    if decode is None:
        return f"M{g['id']} {g.get('mapName')}: winner {g.get('winner')} (decode.py unavailable)", rec
    try:
        d = decode.load(path)
    except Exception as e:  # keep going on a bad replay
        return f"M{g['id']} {g.get('mapName')}: saved, decode failed ({type(e).__name__})", rec
    r = d.get("result") or {}
    a, b = d.get("botA") or "", d.get("botB") or ""
    ours = None
    if a.isdigit() and int(a) in subs:
        ours = "A"
    elif b.isdigit() and int(b) in subs:
        ours = "B"
    sub = a if ours == "A" else b if ours == "B" else ""
    win = r.get("winner")
    verdict = "?" if not ours or win not in ("A", "B") else ("WIN" if win == ours else "LOSS")
    if win not in ("A", "B"):
        verdict = "DRAW" if win else "?"
    mp = decode.GameMap(d["map"]).name if hasattr(decode, "GameMap") else g.get("mapName")
    sa, sb = r.get("A") or {}, r.get("B") or {}
    rec.update(ourSide=ours, ourSubmission=sub, winner=win, endReason=r.get("endReason"),
               verdict=verdict, A=sa, B=sb, botA=a, botB=b)
    subname = subs.get(int(sub)) if sub.isdigit() else None
    return (f"M{g['id']} {mp or g.get('mapName')}: us={ours or '?'}"
            f"{f' ({sub} {subname})' if sub else ''} vs {opp}  winner={win} {verdict}  "
            f"end={r.get('endReason')}  A total {sa.get('total')} / B total {sb.get('total')}"), rec


def cmd_fetch(a):
    pending = fetch(_battle_id(a.battle), a.out, a.force)
    if pending:
        print(f"{pending} game(s) not finished; run `wait` to finish the download.")


def cmd_wait(a):
    bid = _battle_id(a.battle)
    interval = max(WAIT_MIN, a.interval)
    deadline = time.monotonic() + a.timeout * 60
    while True:
        info = _battle_info(bid)
        games = info.get("games") or []
        done = [g for g in games if g.get("status") in ("completed", "failed", "error", "crashed")]
        status = info["battle"]["match"].get("status")
        print(f"[{_now_iso()}] M{bid}: {status}, {len(done)}/{len(games)} games finished",
              flush=True)
        if games and len(done) == len(games):
            break
        if time.monotonic() > deadline:
            raise BcError(f"gave up after {a.timeout} min; M{bid} still {status}")
        time.sleep(interval)
    fetch(bid, a.out)


# ----- challenge / quota ------------------------------------------------------------
# The site's "Request battle" dialog on /teams/<id> reads GET /teams/<id>/availability
# ({"dev", "remaining", "freeIn"}; games left this hour for that opponent class) and submits
# the SvelteKit form action POST /teams/<id>?/challenge with fields kind, ranked, mapId*.
# The allowance counts GAMES: a ranked battle costs 5, an unranked one costs one per map.
# The site always plays our ACTIVE submission; it has no per-request submission choice.


def _log_challenge(rec):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    fd = os.open(CHALLENGE_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _local_log(minutes=60):
    cut = time.time() - minutes * 60
    out = []
    try:
        with open(CHALLENGE_LOG) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= cut:
                    out.append(r)
    except OSError:
        pass
    return out


def availability(team_id):
    for attempt in range(2):
        st, _, data = _request("GET", f"/teams/{team_id}/availability", cookie=_cookie(),
                               headers={"Accept": "application/json"})
        if st in (401, 403) and attempt == 0:
            login(quiet=True)
            continue
        if st != 200:
            raise BcError(f"availability check failed: HTTP {st}")
        try:
            return json.loads(data)
        except ValueError:
            # A lapsed session answers with an HTML login page.
            if attempt == 0:
                login(quiet=True)
                continue
            raise BcError("availability check returned non-JSON")
    raise BcError("availability check failed")


def resolve_team(arg):
    """Team name (case-insensitive, exact then unique prefix), leaderboard rank '#N' or
    'rank:N', or a numeric team id 'id:N'."""
    s = arg.strip()
    if s.lower().startswith("id:"):
        return int(s[3:]), None
    rank = None
    if s.startswith("#"):
        rank = int(s[1:])
    elif s.lower().startswith("rank:"):
        rank = int(s[5:])
    if rank is not None:
        d = page_data("/leaderboard", {"page": (rank - 1) // 10 + 1})
        for t in d.get("teams") or []:
            if t["rank"] == rank:
                return t["id"], t["name"]
        raise BcError(f"no team at rank {rank}")
    teams = page_data("/battles", {"mine": 1}).get("teams") or []
    exact = [t for t in teams if t["name"].lower() == s.lower()]
    if len(exact) == 1:
        return exact[0]["id"], exact[0]["name"]
    pref = [t for t in teams if t["name"].lower().startswith(s.lower())]
    if len(pref) == 1:
        return pref[0]["id"], pref[0]["name"]
    if not pref:
        raise BcError(f"no team named {arg!r}")
    raise BcError(f"{arg!r} is ambiguous: " + ", ".join(t["name"] for t in pref[:10]))


def _form_result(data):
    try:
        r = json.loads(data)
    except ValueError:
        return {"type": "unparsed"}, None
    payload = r.get("data")
    if isinstance(payload, str):
        try:
            payload = _unflatten(json.loads(payload))
        except (ValueError, IndexError, TypeError):
            pass
    return r, payload


def cmd_challenge(a):
    if a.submission is not None:
        raise BcError("the site always plays our ACTIVE submission; it has no way to pick "
                      "another one per battle (activating a different version is out of "
                      "scope for this tool). Drop --submission.")
    tid, name = resolve_team(a.opponent)
    tp = page_data(f"/teams/{tid}")
    team = tp.get("team") or {}
    name = team.get("name") or name
    if tp.get("myTeamId") == tid:
        raise BcError("that is our own team")
    if not team.get("hasBot", tp.get("hasBot", True)):
        raise BcError(f"{name} has no bot")
    kind = "ranked" if a.ranked else "unranked"
    their = team.get("settings") or {}
    if their.get(kind) is False:
        raise BcError(f"{name} does not accept {kind} battle requests")
    all_maps = tp.get("maps") or []
    by_name = {m["name"].lower(): m for m in all_maps}
    chosen = []
    if not a.ranked:
        want = [x.strip() for x in (a.maps or "all").split(",") if x.strip()]
        if want == ["all"]:
            chosen = all_maps[:10]
        else:
            for w in want:
                m = by_name.get(w.lower()) or next(
                    (m for m in all_maps if m["name"].lower().startswith(w.lower())), None)
                if not m:
                    raise BcError(f"unknown map {w!r}; maps in play: "
                                  + ", ".join(m["name"] for m in all_maps))
                if m not in chosen:
                    chosen.append(m)
        if not chosen:
            raise BcError("pick at least one map")
        if len(chosen) > 10:
            raise BcError("an unranked battle takes at most 10 maps")
    elif a.maps:
        raise BcError("ranked battles are 5 games on random maps; --maps is unranked only")
    cost = 5 if a.ranked else len(chosen)

    av = availability(tid)
    rem = av.get("remaining")
    print(f"allowance: {rem} game(s) left this hour"
          f"{' (dev-team pool)' if av.get('dev') else ''}; "
          f"more free in {av.get('freeIn')} min; this request costs {cost}")
    if rem is not None and cost > rem:
        raise BcError(f"not enough allowance: needs {cost} games, {rem} left; "
                      f"more free in {av.get('freeIn')} min. Not retrying.")
    desc = (f"{kind} vs {name} (team {tid})"
            + (f" on {', '.join(m['name'] for m in chosen)}" if chosen else " on 5 random maps"))
    if a.dry_run:
        print("dry run: would request " + desc)
        return

    fields = [("kind", kind), ("ranked", "true" if a.ranked else "false")]
    fields += [("mapId", str(m["id"])) for m in chosen]
    body = urllib.parse.urlencode(fields).encode()
    st, _, data = _request("POST", f"/teams/{tid}?/challenge", body=body, cookie=_cookie(),
                           tries=1,
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Accept": "application/json", "Origin": BASE,
                                    "Referer": f"{BASE}/teams/{tid}",
                                    "x-sveltekit-action": "true"})
    r, payload = _form_result(data)
    rec = {"ts": time.time(), "at": _now_iso(), "opponent": name, "teamId": tid,
           "ranked": bool(a.ranked), "maps": [m["name"] for m in chosen], "cost": cost}
    if st == 429:
        raise BcError("the site refused: rate limited (HTTP 429). Not retrying.")
    if r.get("type") == "success":
        bid = (payload or {}).get("battleId") if isinstance(payload, dict) else None
        rec["battle"] = bid
        _log_challenge(rec)
        print(f"queued {desc}")
        print(f"battle id: M{bid}" if bid else "battle queued (no id returned; see `battles`)")
        return
    if r.get("type") == "redirect" and "/login" in str(r.get("location")):
        raise BcError("session expired; run `login` and try again")
    msg = payload.get("message") if isinstance(payload, dict) else None
    raise BcError(f"the site refused the request (HTTP {st}, {r.get('type')})"
                  + (f": {msg}" if msg else "") + ". Not retrying.")


def cmd_quota(a):
    if a.opponent:
        tid, name = resolve_team(a.opponent)
    else:
        # Any non-dev opponent reports the shared pool; use the team we last played.
        bs, myid = _my_battles(1)
        if not bs:
            raise BcError("no battles yet; pass an opponent")
        _, them, *_ = _perspective(bs[0], myid)
        tid, name = them["id"], them["name"]
    av = availability(tid)
    pool = "dev-team pool" if av.get("dev") else "shared pool (all non-dev opponents)"
    print(f"{av.get('remaining')} game(s) left this hour in the {pool}; "
          f"more free in {av.get('freeIn')} min. (ranked = 5 games, unranked = 1 per map; "
          f"asked via {name})")
    recent = _local_log(60)
    print(f"our local log: {len(recent)} request(s), "
          f"{sum(r.get('cost', 0) for r in recent)} game(s) started by bcweb in the last 60 min "
          f"(teammates and the website are not in this log)")


# ----- tournaments ------------------------------------------------------------------
# /tournaments/__data.json lists every tournament with its full bracket inline:
# rounds[r][i] = {a, b, winner, bye, battleId, games:[{id, winner, map}]}; a/b carry
# {teamId, name, elo, seed, score}. losersRounds holds a double-elimination lower bracket.
# Game winners 'a'/'b' are bracket slots, not replay seats (seats can swap per game).

MAPS_TOURNAMENT = os.path.join(LEAGUE_ROOT, "maps-tournament")
OUR_TEAM_ID = 328


def _tournaments():
    return page_data("/tournaments").get("tournaments") or []


def _tournament(tid):
    ts = _tournaments()
    for t in ts:
        if tid.lower() in (str(t.get("id")).lower(), str(t.get("name")).lower(),
                           str(t.get("kind")).lower()):
            return t
    raise BcError(f"no tournament {tid!r}; known: " + ", ".join(str(t.get("id")) for t in ts))


def _round_name(n_matches, idx, lower=False):
    if lower:
        return f"L{idx + 1}"
    return {1: "final", 2: "semifinal", 4: "quarterfinal"}.get(n_matches, f"round of {2 * n_matches}")


def _t_matches(t):
    """Yield (label, round_index, match) for every played (non-bye) match."""
    for lower, key in ((False, "rounds"), (True, "losersRounds")):
        for ri, rnd in enumerate(t.get(key) or []):
            label = _round_name(len(rnd), ri, lower)
            for m in rnd:
                if m and not m.get("bye") and (m.get("a") and m.get("b")):
                    yield label, ri, lower, m


def _side_str(x, w, slot):
    if not x:
        return "-"
    sc = x.get("score")
    return (f"{'>' if w == slot else ' '}{x.get('name')} (#{x.get('seed')})"
            + (f" {sc}" if sc is not None else ""))


def _involves(m, team):
    team = team.lower()
    return any(m.get(s) and (str(m[s].get("teamId")) == team or
                             str(m[s].get("name", "")).lower() == team) for s in "ab")


def cmd_tournaments(a):
    rows = []
    for t in _tournaments():
        played = sum(1 for _ in _t_matches(t))
        games = sum(len(m.get("games") or []) for *_, m in _t_matches(t))
        rows.append([t.get("id"), t.get("name"), t.get("status"), _short_time(t.get("date")),
                     t.get("size"), f"bo{t.get('bestOf')}", len(t.get("rounds") or []),
                     played, games])
    _table(rows, ["id", "name", "status", "date(UTC)", "teams", "format", "rounds",
                  "matches", "games"])
    print("(tabs on the site with no entry here, e.g. Qualifiers or Grand Final, "
          "have no data yet)")


def cmd_bracket(a):
    t = _tournament(a.tournament)
    allm = list(_t_matches(t))
    nrounds = len(t.get("rounds") or [])
    show_from = 0 if a.all else max(0, nrounds - a.top)
    print(f"{t.get('name')} ({t.get('id')}): {t.get('status')}, {t.get('size')} teams, "
          f"best of {t.get('bestOf')}, {nrounds} rounds, {len(allm)} matches played")
    maps = {}
    for *_, m in allm:
        for g in m.get("games") or []:
            maps[g.get("map")] = maps.get(g.get("map"), 0) + 1
    print("maps: " + ", ".join(f"{k} ({v})" for k, v in maps.items()))
    cur = None
    for label, ri, lower, m in allm:
        mine = _involves(m, a.team) if a.team else _involves(m, str(OUR_TEAM_ID))
        if not (mine or ri >= show_from or a.all or lower):
            continue
        if (label, lower) != cur:
            cur = (label, lower)
            print(f"\n== {label}" + (" (lower bracket)" if lower else ""))
        w = m.get("winner")
        gs = " ".join(f"{(g.get('map') or '?')[:10]}:{g.get('winner')}"
                      for g in m.get("games") or [])
        star = "*" if mine else " "
        print(f"{star}M{m.get('battleId')}  {_side_str(m.get('a'), w, 'a'):<38} vs "
              f"{_side_str(m.get('b'), w, 'b'):<38} {gs}")
    print("\n> = match winner; game list is map:winner-slot; * = "
          + (a.team if a.team else "our matches") + f". Showing the last {a.top} rounds "
          "plus starred matches; --all for every match.")


def _seat_of_a(m_games, decoded):
    """Work out which replay seat (A/B) bracket slot 'a' held in each game."""
    out = {}
    for g in m_games:
        d = decoded.get(g["id"])
        if not d:
            continue
        rw, bw = (d.get("result") or {}).get("winner"), g.get("winner")
        if rw in ("A", "B") and bw in ("a", "b"):
            out[g["id"]] = rw if bw == "a" else ("B" if rw == "A" else "A")
    return out


def tfetch(t, matches, root, force=False, maps_out=None):
    decode = _load_decode()
    subs = {s["id"]: s.get("name") for s in _submissions()}
    seen_maps = {}
    n_games = 0
    for label, ri, lower, m in matches:
        bid = m.get("battleId")
        out_dir = os.path.join(root, f"M{bid}")
        os.makedirs(out_dir, exist_ok=True)
        A, B = m.get("a") or {}, m.get("b") or {}
        print(f"M{bid} [{label}{' lower' if lower else ''}] {A.get('name')} {A.get('score')}"
              f" - {B.get('score')} {B.get('name')}")
        decoded, recs = {}, []
        for g in m.get("games") or []:
            gid = g["id"]
            path = os.path.join(out_dir, f"M{gid}.replay")
            if force or not os.path.exists(path):
                data = _download_game(gid)
                if data is None:
                    print(f"  M{gid} {g.get('map')}: not finished")
                    continue
                with open(path, "wb") as f:
                    f.write(data)
            n_games += 1
            if decode is not None:
                try:
                    decoded[gid] = decode.load(path)
                except Exception as e:
                    print(f"  M{gid} {g.get('map')}: saved, decode failed ({type(e).__name__})")
        seats = _seat_of_a(m.get("games") or [], decoded)
        for g in m.get("games") or []:
            gid = g["id"]
            d = decoded.get(gid)
            name_w = (A if g.get("winner") == "a" else B).get("name")
            if not d:
                if os.path.exists(os.path.join(out_dir, f"M{gid}.replay")) and decode is None:
                    print(f"  M{gid} {g.get('map')}: winner {name_w}")
                continue
            r = d.get("result") or {}
            sa = seats.get(gid)
            seat_names = ({sa: A.get("name"), ("B" if sa == "A" else "A"): B.get("name")}
                          if sa else {})
            ours = None
            for seat, bot in (("A", d.get("botA") or ""), ("B", d.get("botB") or "")):
                if bot.isdigit() and int(bot) in subs:
                    ours = seat
            if ours is None and sa:
                # Tournament replays carry team names, not submission ids, in botA/botB.
                if A.get("teamId") == OUR_TEAM_ID:
                    ours = sa
                elif B.get("teamId") == OUR_TEAM_ID:
                    ours = "B" if sa == "A" else "A"
            ra, rb = r.get("A") or {}, r.get("B") or {}
            us = (f" us={ours}" + (" WIN" if r.get("winner") == ours else " LOSS")) if ours else ""
            print(f"  M{gid} {g.get('map')}: A={seat_names.get('A', '?')} "
                  f"B={seat_names.get('B', '?')} winner={r.get('winner')} ({name_w}) "
                  f"end={r.get('endReason')} A total {ra.get('total')} / B total "
                  f"{rb.get('total')}{us}")
            recs.append({"id": gid, "map": g.get("map"), "siteWinnerSlot": g.get("winner"),
                         "seatOfA": sa, "winner": r.get("winner"),
                         "endReason": r.get("endReason"), "A": ra, "B": rb, "ourSide": ours,
                         "botA": d.get("botA"), "botB": d.get("botB"), "seed": d.get("seed")})
            mname = g.get("map") or "unknown"
            if mname not in seen_maps and d.get("map"):
                seen_maps[mname] = (d["map"], gid)
        with open(os.path.join(out_dir, "battle.json"), "w") as f:
            json.dump({"tournament": t.get("id"), "round": label, "lower": lower,
                       "battle": bid, "a": A, "b": B, "winner": m.get("winner"),
                       "fetchedAt": _now_iso(), "games": recs}, f, indent=1, default=str)
    if maps_out and seen_maps:
        _save_maps(seen_maps, maps_out)
    return n_games


def _known_map_names():
    names = set()
    repo = os.path.dirname(os.path.dirname(HERE))
    for sub in ("maps/official", "maps/server"):
        d = os.path.join(repo, sub)
        for fn in os.listdir(d) if os.path.isdir(d) else []:
            if not fn.endswith(".map"):
                continue
            try:
                with open(os.path.join(d, fn)) as f:
                    for line in f:
                        if line.startswith("MAP_NAME"):
                            names.add(line.split(None, 1)[1].strip().lower())
                            break
            except OSError:
                pass
            names.add(fn[:-4].lower())
    return names


def _slug(name):
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")


def _save_maps(seen, out_dir):
    known = _known_map_names()
    os.makedirs(out_dir, exist_ok=True)
    for name, (text, gid) in sorted(seen.items()):
        if name.lower() in known or _slug(name) in known:
            print(f"map {name}: already in maps/official or maps/server")
            continue
        path = os.path.join(out_dir, _slug(name) + ".map")
        with open(path, "w") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        print(f"map {name}: NEW, rebuilt from replay M{gid} -> {path}")


def cmd_tfetch(a):
    t = _tournament(a.tournament)
    allm = list(_t_matches(t))
    nrounds = len(t.get("rounds") or [])
    if a.all:
        sel = allm
    elif a.team:
        sel = [x for x in allm if _involves(x[3], a.team)]
    else:
        sel = [x for x in allm
               if x[1] >= nrounds - a.top or x[2] or _involves(x[3], str(OUR_TEAM_ID))]
    if not sel:
        raise BcError("no matches selected")
    ng = sum(len(x[3].get("games") or []) for x in sel)
    print(f"{t.get('name')}: {len(sel)} match(es), {ng} game(s) selected")
    if a.all and ng > 500 and not a.yes:
        raise BcError(f"--all is {ng} games (several GB, about {ng // 60 + 1} min at our "
                      "request rate); pass --yes to confirm")
    root = a.out or os.path.join(REPLAY_ROOT, f"tournament-{_slug(str(t.get('id')))}")
    maps_out = os.path.join(MAPS_TOURNAMENT, _slug(str(t.get("id"))))
    n = tfetch(t, sel, root, a.force, maps_out)
    print(f"{n} replay(s) in {root}")


# --------------------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(prog="bcweb.py", description=__doc__.split("\n\n")[0])
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("login", help="authenticate and cache the session").set_defaults(f=cmd_login)
    sp.add_parser("whoami", help="show our user and team").set_defaults(f=cmd_whoami)
    s = sp.add_parser("submissions", help="list our submissions; * marks the live one")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--maps", action="store_true", help="also print per-map W-D-L")
    s.set_defaults(f=cmd_submissions)
    s = sp.add_parser("leaderboard", help="current rankings, our team marked")
    s.add_argument("--pages", type=int, default=3, help="pages of 10 to read (default 3)")
    s.set_defaults(f=cmd_leaderboard)
    s = sp.add_parser("battles", help="our recent battles")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--maps", action="store_true",
                   help="also list per-game maps (one extra request per battle)")
    s.set_defaults(f=cmd_battles)
    s = sp.add_parser("fetch", help="download a battle's replays and summarise each game")
    s.add_argument("battle", help="battle id, e.g. 727546 or M727546")
    s.add_argument("--out", help=f"replay root (default {REPLAY_ROOT})")
    s.add_argument("--force", action="store_true", help="re-download existing files")
    s.set_defaults(f=cmd_fetch)
    s = sp.add_parser("wait", help="poll until a battle finishes, then fetch it")
    s.add_argument("battle")
    s.add_argument("--interval", type=int, default=WAIT_MIN, help="seconds, minimum 60")
    s.add_argument("--timeout", type=int, default=180, help="minutes before giving up")
    s.add_argument("--out")
    s.set_defaults(f=cmd_wait)
    s = sp.add_parser("challenge", help="request a battle (unranked by default)")
    s.add_argument("opponent", help="team name, '#RANK' / 'rank:N', or 'id:TEAMID'")
    s.add_argument("--submission", type=int,
                   help="not supported by the site (it always plays the active version)")
    s.add_argument("--ranked", action="store_true",
                   help="ranked: 5 games on random maps, changes ratings")
    s.add_argument("--maps", help="unranked: comma-separated map names (prefixes ok), "
                                  "up to 10, or 'all' (default: all maps in play)")
    s.add_argument("--dry-run", action="store_true", help="check everything, send nothing")
    s.set_defaults(f=cmd_challenge)
    s = sp.add_parser("quota", help="games left in the site's hourly allowance")
    s.add_argument("opponent", nargs="?", help="ask about this opponent's pool (default: "
                                                "our last opponent)")
    s.set_defaults(f=cmd_quota)
    sp.add_parser("tournaments", help="list tournaments and status").set_defaults(
        f=cmd_tournaments)
    s = sp.add_parser("bracket", help="a tournament's rounds, matches, results and maps")
    s.add_argument("tournament", help="id or name, e.g. sprint")
    s.add_argument("--top", type=int, default=4, help="show the last N rounds (default 4)")
    s.add_argument("--team", help="star this team's matches (name or team id)")
    s.add_argument("--all", action="store_true", help="every match")
    s.set_defaults(f=cmd_bracket)
    s = sp.add_parser("tfetch", help="download a tournament's replays (+ new maps)")
    s.add_argument("tournament")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="every played match")
    g.add_argument("--team", help="only this team's matches (name or team id)")
    s.add_argument("--top", type=int, default=4,
                   help="default selection: the last N rounds plus our matches (default 4)")
    s.add_argument("--yes", action="store_true", help="confirm a very large --all")
    s.add_argument("--force", action="store_true")
    s.add_argument("--out", help="replay root (default replays/tournament-<id>)")
    s.set_defaults(f=cmd_tfetch)
    a = p.parse_args(argv)
    try:
        a.f(a)
    except BcError as e:
        print(f"bcweb: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
