"""Reads unswbc .replay files (packed Cap'n Proto; the schema is the one in the toolkit's
replay viewer bundle) into the verdict and stats a local game gets from the judge's log.

Server replays come gzip-compressed; `gunzip()` undoes that, and the arena stores them
uncompressed like the judge's own. The stats match stats.extract() where the replay has the
same facts (deaths by cause, moves, sprints, splits, sonars, dragons alive every 10 rounds,
the tiebreak) and leave out what only the judge's log has (CPU points).

Standard library only. Adapted from syeyoung-tokens' league analysis decoder.
"""

from __future__ import annotations

import gzip
import struct

DIRS = "NESW"
# Replay death kinds, worded as the judge prints them (unswbc/run.py DEATH_REASONS).
DEATHS = ["hit a wall", "hit itself", "hit another dragon", "lost a head-to-head", "no valid action"]


def gunzip(blob: bytes) -> bytes:
    return gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob


def _unpack(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        tag = data[i]
        i += 1
        if tag == 0:
            out += b"\0" * (8 * (data[i] + 1))
            i += 1
        elif tag == 0xFF:
            out += data[i:i + 8]
            i += 8
            cnt = data[i]
            i += 1
            out += data[i:i + 8 * cnt]
            i += 8 * cnt
        else:
            for b in range(8):
                if tag & (1 << b):
                    out.append(data[i])
                    i += 1
                else:
                    out.append(0)
    return bytes(out)


class _Msg:
    def __init__(self, raw: bytes):
        nseg = struct.unpack_from("<I", raw, 0)[0] + 1
        sizes = struct.unpack_from(f"<{nseg}I", raw, 4)
        off = 4 + 4 * nseg
        off += off % 8
        self.segs = []
        for s in sizes:
            self.segs.append(raw[off:off + 8 * s])
            off += 8 * s

    def word(self, seg, idx):
        return struct.unpack_from("<Q", self.segs[seg], idx * 8)[0]

    def resolve(self, seg, pidx):
        w = self.word(seg, pidx)
        if w == 0:
            return None
        if w & 3 == 2:                       # far pointer
            landing, tseg = (w >> 3) & ((1 << 29) - 1), w >> 32
            if not (w >> 2) & 1:
                return self.resolve(tseg, landing)
            w1, tag = self.word(tseg, landing), self.word(tseg, landing + 1)
            return (tag & 3, w1 >> 32, (w1 >> 3) & ((1 << 29) - 1), tag)
        off = (w >> 2) & ((1 << 30) - 1)
        if off & (1 << 29):
            off -= 1 << 30
        return (w & 3, seg, pidx + 1 + off, w)


class _Struct:
    def __init__(self, msg, seg, start, dwords, pcount):
        self.m, self.seg, self.start, self.dw, self.pc = msg, seg, start, dwords, pcount

    def _d(self, fmt, byteoff):
        if byteoff + struct.calcsize(fmt) > self.dw * 8:
            return 0
        return struct.unpack_from("<" + fmt, self.m.segs[self.seg], self.start * 8 + byteoff)[0]

    def u16(self, o): return self._d("H", o)
    def i32(self, o): return self._d("i", o)
    def u64(self, o): return self._d("Q", o)

    def bit(self, n):
        return (self._d("B", n // 8) >> (n % 8)) & 1

    def _ptr(self, i):
        return None if i >= self.pc else self.m.resolve(self.seg, self.start + self.dw + i)

    def struct(self, i):
        r = self._ptr(i)
        if r is None:
            return None
        _, seg, tgt, w = r
        return _Struct(self.m, seg, tgt, (w >> 32) & 0xFFFF, (w >> 48) & 0xFFFF)

    def text(self, i):
        r = self._ptr(i)
        if r is None:
            return ""
        _, seg, tgt, w = r
        return self.m.segs[seg][tgt * 8: tgt * 8 + (w >> 35) - 1].decode("utf-8", "replace")

    def list_u16(self, i):
        r = self._ptr(i)
        if r is None:
            return []
        _, seg, tgt, w = r
        return list(struct.unpack_from(f"<{w >> 35}H", self.m.segs[seg], tgt * 8))

    def list_struct(self, i):
        r = self._ptr(i)
        if r is None:
            return []
        _, seg, tgt, w = r
        tag = self.m.word(seg, tgt)
        n, dw, pc = (tag >> 2) & ((1 << 30) - 1), (tag >> 32) & 0xFFFF, (tag >> 48) & 0xFFFF
        return [_Struct(self.m, seg, tgt + 1 + k * (dw + pc), dw, pc) for k in range(n)]


def _event(e):
    """The events the stats need, as tuples; everything else as (kind number,)."""
    t = e.u16(0)
    p = e.struct(0)
    if p is None:
        return (t,)
    if t == 0:
        return ("round", p.i32(0))
    if t == 1:
        return ("turn", p.i32(0))
    if t == 4:
        a = p.struct(0)
        act = None
        if a is not None:
            kind = a.u16(0)
            act = (("move", "".join(DIRS[d % 4] for d in a.list_u16(0))) if kind == 0
                   else ("split",) if kind == 1 else ("suicide",))
        return ("action", p.i32(0), act, p.bit(32))
    if t == 10:
        return ("split", p.i32(0), p.i32(4), "AB"[p.u16(8) & 1])
    if t == 11:
        k = p.u16(4)
        return ("death", p.i32(0), DEATHS[k] if k < len(DEATHS) else f"death {k}")
    if t == 12:
        return ("sonar", p.i32(0))
    return (t,)


def load(blob: bytes) -> dict:
    """{map, botA, botB, seed, result, events} from a replay file (gzip or not)."""
    raw = _unpack(gunzip(blob))
    m = _Msg(raw)
    _, seg, tgt, w = m.resolve(0, 0)
    r = _Struct(m, seg, tgt, (w >> 32) & 0xFFFF, (w >> 48) & 0xFFFF)
    res = r.struct(4)

    def standing(s):
        return {"count": s.i32(0), "longest": s.i32(4), "total": s.i32(8),
                "queen": s.i32(12)} if s else None

    result = {"terminated": bool(res.bit(0)),
              "end": ["teamEliminated", "roundLimit"][res.u16(2) & 1],
              "winner": "AB"[res.u16(6) & 1] if res.u16(4) == 1 else None,
              "A": standing(res.struct(0)), "B": standing(res.struct(1))}
    return {"map": r.text(0), "botA": r.text(1), "botB": r.text(2), "format": r._d("I", 0),
            "seed": r.u64(8) if r.u16(4) == 1 else None, "result": result,
            "events": [_event(e) for e in r.list_struct(3)]}


def verdict(result: dict) -> str:
    """The end reason worded as the local runner stores it (unswbc's _verdict, minus 'by ')."""
    a, b, w = result.get("A") or {}, result.get("B") or {}, result.get("winner")
    if result.get("end") == "teamEliminated":
        return "elimination" if w else "both teams eliminated"
    ka = (a.get("queen", 0), a.get("longest", 0), a.get("total", 0))
    kb = (b.get("queen", 0), b.get("longest", 0), b.get("total", 0))
    if w is None:
        return f"equal length: queen {ka[0]}, longest {ka[1]}, total {ka[2]} each"
    won, lost = (ka, kb) if w == "A" else (kb, ka)
    for label, x, y in zip(("longer queen", "longest dragon", "total length"), won, lost):
        if x != y:
            return f"{label}, {x} to {y}"
    return "on length"


def _map_dragons(text: str) -> list[str]:
    """Each initial dragon's team, in id order ('DRAGON <team> ...' lines)."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if p and p[0] in ("DRAGON", "SNAKE") and len(p) > 1 and p[1].isdigit():
            out.append("AB"[int(p[1]) & 1])
    return out


def summarize(d: dict) -> dict:
    """{winner, rounds, reason, stats} for an arena Game, from a loaded replay."""
    team = dict(enumerate(_map_dragons(d["map"])))
    alive = set(team)
    teams = {t: {"deaths": {}, "bots": set(), "turns": 0, "moves": 0, "sprints": 0, "splits": 0,
                 "sonars": 0, "errors": 0, "points_max": 0} for t in "AB"}
    series, rounds, turn_no, sonar_turn = [], 0, 0, -1
    for ev in d["events"]:
        k = ev[0]
        if k == "round":
            rounds += 1
            r = ev[1]
            if r % 10 == 0 or r == 1:
                cnt = {"A": 0, "B": 0}
                for i in alive:
                    cnt[team.get(i, "A")] += 1
                series.append([r, cnt["A"], cnt["B"]])
        elif k == "turn":
            turn_no += 1
            t = teams[team.get(ev[1], "A")]
            t["turns"] += 1
            t["bots"].add(ev[1])
        elif k == "action":
            t = teams[team.get(ev[1], "A")]
            if ev[3]:
                t["errors"] += 1           # the turn ran out of time
            if ev[2] and ev[2][0] == "move":
                t["moves"] += 1
                t["sprints"] += len(ev[2][1]) > 1
        elif k == "split":
            team[ev[2]] = ev[3]
            alive.add(ev[2])
            teams[ev[3]]["splits"] += 1
        elif k == "death":
            alive.discard(ev[1])
            dd = teams[team.get(ev[1], "A")]["deaths"]
            dd[ev[2]] = dd.get(ev[2], 0) + 1
        elif k == "sonar" and sonar_turn != turn_no:     # one SONAR can ping several times
            sonar_turn = turn_no
            teams[team.get(ev[1], "A")]["sonars"] += 1
    res = d["result"]
    reason = verdict(res)
    stats = {"teams": {}, "dragons": series, "source": "replay",
             "standings": {"A": res.get("A"), "B": res.get("B")}}
    for k, t in teams.items():
        t = dict(t)
        t["dragons_used"] = len(t.pop("bots"))
        t["died"] = sum(t["deaths"].values())
        t["points"] = {}
        stats["teams"][k] = t
    if res.get("winner") and ", " in reason and " to " in reason:
        by, nums = reason.rsplit(", ", 1)
        x, y = nums.split(" to ")
        stats["tiebreak"] = {"by": by, "winner": int(x), "loser": int(y)}
    return {"winner": res.get("winner"), "rounds": rounds, "reason": reason, "stats": stats}
