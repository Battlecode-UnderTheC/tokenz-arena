"""Replays a .replay file into game state, round by round (for arena/metrics).

  d = load_bytes(blob)            # dict: map, botA, botB, result, events, seed
  g = GameMap(d["map"])           # W, H, stepping through kelp and portals (g.step), initial dragons
  for kind, st, info in walk(d):  # 'pre', 'turn', 'death', 'split', 'round_end' (see walk())

Team A is the first DRAGON in the map (maps alternate teams, so dragons 0 and 1 are the two
queens). Standard library only. Adapted from the eval-bar analysis decoder, which came from
syeyoung-tokens' league analysis.
"""
import collections
import struct


def unpack(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        tag = data[i]; i += 1
        if tag == 0:
            cnt = data[i]; i += 1
            out += b"\0" * (8 * (cnt + 1))
        elif tag == 0xFF:
            out += data[i:i + 8]; i += 8
            cnt = data[i]; i += 1
            out += data[i:i + 8 * cnt]; i += 8 * cnt
        else:
            for b in range(8):
                if tag & (1 << b):
                    out.append(data[i]); i += 1
                else:
                    out.append(0)
    return bytes(out)


class Msg:
    def __init__(self, raw: bytes):
        nseg = struct.unpack_from("<I", raw, 0)[0] + 1
        sizes = struct.unpack_from("<%dI" % nseg, raw, 4)
        off = 4 + 4 * nseg
        off += off % 8
        self.segs = []
        for s in sizes:
            self.segs.append(raw[off:off + 8 * s])
            off += 8 * s

    def word(self, seg, idx):
        return struct.unpack_from("<Q", self.segs[seg], idx * 8)[0]

    def resolve(self, seg, pidx):
        """Resolve pointer at (seg, word pidx). Returns (kind, seg, target_word, ptrword) or None."""
        w = self.word(seg, pidx)
        if w == 0:
            return None
        kind = w & 3
        if kind == 2:  # far
            double = (w >> 2) & 1
            landing = (w >> 3) & ((1 << 29) - 1)
            tseg = (w >> 32)
            if not double:
                return self.resolve(tseg, landing)
            # double-far: landing pad is far ptr to content + tag word
            w1 = self.word(tseg, landing)
            cseg = w1 >> 32
            cstart = (w1 >> 3) & ((1 << 29) - 1)
            tag = self.word(tseg, landing + 1)
            return (tag & 3, cseg, cstart, tag)
        off = (w >> 2) & ((1 << 30) - 1)
        if off & (1 << 29):
            off -= 1 << 30
        return (kind, seg, pidx + 1 + off, w)


class Struct:
    def __init__(self, msg, seg, start, dwords, pcount):
        self.m, self.seg, self.start, self.dw, self.pc = msg, seg, start, dwords, pcount

    def _d(self, fmt, byteoff):
        size = struct.calcsize(fmt)
        if byteoff + size > self.dw * 8:
            return 0
        return struct.unpack_from("<" + fmt, self.m.segs[self.seg], self.start * 8 + byteoff)[0]

    def u16(self, o): return self._d("H", o)
    def i32(self, o): return self._d("i", o)
    def u32(self, o): return self._d("I", o)
    def u64(self, o): return self._d("Q", o)

    def bit(self, n):
        return (self._d("B", n // 8) >> (n % 8)) & 1

    def _ptr(self, i):
        if i >= self.pc:
            return None
        return self.m.resolve(self.seg, self.start + self.dw + i)

    def struct(self, i):
        r = self._ptr(i)
        if r is None:
            return None
        kind, seg, tgt, w = r
        assert kind == 0
        return Struct(self.m, seg, tgt, (w >> 32) & 0xFFFF, (w >> 48) & 0xFFFF)

    def text(self, i):
        r = self._ptr(i)
        if r is None:
            return ""
        kind, seg, tgt, w = r
        n = (w >> 35)
        return self.m.segs[seg][tgt * 8: tgt * 8 + n - 1].decode("utf-8", "replace")

    def list_u16(self, i):
        r = self._ptr(i)
        if r is None:
            return []
        kind, seg, tgt, w = r
        n = w >> 35
        return list(struct.unpack_from("<%dH" % n, self.m.segs[seg], tgt * 8))

    def list_struct(self, i):
        r = self._ptr(i)
        if r is None:
            return []
        kind, seg, tgt, w = r
        esz = (w >> 32) & 7
        assert esz == 7, esz
        tag = self.m.word(seg, tgt)
        n = (tag >> 2) & ((1 << 30) - 1)
        dw = (tag >> 32) & 0xFFFF
        pc = (tag >> 48) & 0xFFFF
        step = dw + pc
        return [Struct(self.m, seg, tgt + 1 + k * step, dw, pc) for k in range(n)]


def root(raw):
    m = Msg(raw)
    kind, seg, tgt, w = m.resolve(0, 0)
    return Struct(m, seg, tgt, (w >> 32) & 0xFFFF, (w >> 48) & 0xFFFF)


DIRS = "NESW"
DEATH = ["wall", "self", "other_body", "head_to_head", "no_valid_action"]
HITKIND = [None, "empty", "kelp", "ally", "allyHead", "enemy", "enemyHead"]


def pt(s):
    return (s.i32(0), s.i32(4)) if s else None


def decode_event(e, fv):
    t = e.u16(0)
    p = e.struct(0)
    if t == 0:
        return ("roundStart", p.i32(0))
    if t == 1:
        return ("turnStart", p.i32(0))
    if t == 2:
        return ("pearlCountdown", pt(p.struct(0)), p.i32(0))
    if t == 3:
        return ("tileChange", pt(p.struct(0)), p.bit(0))
    if t == 4:
        a = p.struct(0)
        act = None
        if a is not None:
            w = a.u16(0)
            if w == 0:
                act = ("move", "".join(DIRS[d] for d in a.list_u16(0)))
            elif w == 1:
                act = ("split", a.i32(4))
            else:
                act = ("suicide",)
        return ("dragonAction", p.i32(0), act, p.bit(32))
    if t == 5:
        return ("engineLog", p.i32(0), p.text(0))
    if t == 6:
        return ("dragonLog", p.i32(0), p.text(0))
    if t == 7:
        return ("dragonIndicator", p.i32(0), p.text(0))
    if t == 8:
        return ("debugDraw", p.i32(0))
    if t == 9:
        return ("dragonUpdate", p.i32(0), DIRS[p.u16(4)], pt(p.struct(0)), pt(p.struct(1)))
    if t == 10:
        return ("dragonSplit", p.i32(0), p.i32(4), "AB"[p.u16(8)], DIRS[p.u16(10)],
                [pt(x) for x in p.list_struct(0)], [pt(x) for x in p.list_struct(1)])
    if t == 11:
        return ("dragonDeath", p.i32(0), DEATH[p.u16(4)])
    if t == 12:
        hit = p.i32(12) if p.u16(6) == 1 else None
        val = p.u32(8) if fv < 2 else p.u64(16)
        return ("sonarPing", p.i32(0), DIRS[p.u16(4)], val, pt(p.struct(0)), pt(p.struct(1)), hit,
                HITKIND[p.u16(24)] if p.u16(24) < len(HITKIND) else p.u16(24))
    return ("unknown", t)




def load_bytes(blob: bytes) -> dict:
    """A replay file's contents (plain or gzip-compressed, as the arena stores them)."""
    import gzip as _gz
    if blob[:2] == b"\x1f\x8b":
        blob = _gz.decompress(blob)
    return _load_raw(blob)


def load(path):
    with open(path, "rb") as f:
        return load_bytes(f.read())


def _load_raw(blob: bytes) -> dict:
    raw = unpack(blob)
    r = root(raw)
    fv = r.u32(0)
    res = r.struct(4)
    ta, tb = res.struct(0), res.struct(1)
    stand = lambda s: dict(count=s.i32(0), longest=s.i32(4), total=s.i32(8), queen=s.i32(12)) if s else None
    result = dict(terminated=res.bit(0), endReason=["teamEliminated", "roundLimit"][res.u16(2)],
                  winner=("AB"[res.u16(6)] if res.u16(4) == 1 else None),
                  A=stand(ta), B=stand(tb))
    events = [decode_event(e, fv) for e in r.list_struct(3)]
    return dict(map=r.text(0), botA=r.text(1), botB=r.text(2), formatVersion=fv,
                seed=(r.u64(8) if r.u16(4) == 1 else None), result=result, events=events)


# ---- the map --------------------------------------------------------------------------------------

DXY = {"N": (0, -1), "S": (0, 1), "E": (1, 0), "W": (-1, 0)}
OPP = {"N": "S", "S": "N", "E": "W", "W": "E"}


class GameMap:
    """Map text -> torus with kelp and portals. Tiles are (x, y) tuples."""

    def __init__(self, text):
        self.name, self.symmetry, self.unit_limit = "?", "-", 64
        self.edges = {}          # edge id -> (type, portal id)
        self.dragons = []        # (team 'A'/'B', [(x, y), ...] head first)
        self.bed_ranges = {}     # (x, y) -> (min, max) when the map text carries them (server replays zero them)
        portals = {}
        for line in text.splitlines():
            p = line.split()
            if not p:
                continue
            k = p[0]
            if k == "MAP":
                self.W, self.H = int(p[1]), int(p[2])
            elif k == "MAP_NAME":
                self.name = line[9:].strip()
            elif k == "SYMMETRY":
                self.symmetry = p[1]
            elif k == "UNIT_LIMIT":
                self.unit_limit = int(p[1])
            elif k == "TILE":
                if int(p[4]) > 0:
                    self.bed_ranges[(int(p[1]), int(p[2]))] = (int(p[3]), int(p[4]))
            elif k == "EDGE":
                eid, typ, pid = int(p[1]), int(p[2]), int(p[3])
                self.edges[eid] = (typ, pid)
                if typ == 2:
                    portals.setdefault(pid, []).append(eid)
            elif k in ("DRAGON", "SNAKE"):
                v = list(map(int, p[1:]))
                self.dragons.append(("AB"[v[0]], [(v[2 + 2 * i], v[3 + 2 * i]) for i in range(v[1])]))
        self.partner = {}
        for pid, es in portals.items():
            if len(es) == 2:
                self.partner[es[0]] = es[1]
                self.partner[es[1]] = es[0]
        self.kelp_count = sum(1 for t, _ in self.edges.values() if t == 1)
        self.portal_count = len(portals)
        self._step = {}
        for y in range(self.H):
            for x in range(self.W):
                for d in "NESW":
                    self._step[((x, y), d)] = self._compute_step(x, y, d)

    def edge_id(self, x, y, d):
        W, H = self.W, self.H
        if d == "N":
            return 2 * y * (W + 1) + x
        if d == "S":
            return 2 * ((y + 1) % H) * (W + 1) + x
        if d == "W":
            return (2 * y + 1) * (W + 1) + x
        return (2 * y + 1) * (W + 1) + (x + 1) % W

    def _edge_tile(self, eid):
        r = self.W + 1
        i, a = divmod(eid, r)
        if i % 2 == 0:
            return "H", a % self.W, (i // 2) % self.H      # north side of (a, i/2)
        return "V", a % self.W, ((i - 1) // 2) % self.H      # west side of (a, (i-1)/2)

    def _compute_step(self, x, y, d):
        eid = self.edge_id(x, y, d)
        typ, _ = self.edges.get(eid, (0, -1))
        if typ == 1:
            return None
        if typ == 2 and eid in self.partner:
            o, px, py = self._edge_tile(self.partner[eid])
            if d == "N":
                return (px, (py - 1) % self.H)
            if d == "S":
                return (px, py)
            if d == "W":
                return ((px - 1) % self.W, py)
            return (px, py)
        dx, dy = DXY[d]
        return ((x + dx) % self.W, (y + dy) % self.H)

    def step(self, tile, d):
        """The tile a step from `tile` heading `d` lands on (portals followed), or None for kelp."""
        return self._step[(tile, d)]

    def bfs(self, src, blocked=(), limit=10 ** 9):
        """Step distances from src over the map (kelp and `blocked` tiles impassable), up to `limit`."""
        dist = {src: 0}
        frontier = [src]
        dd = 0
        while frontier and dd < limit:
            dd += 1
            nxt = []
            for t in frontier:
                for d in "NESW":
                    n = self._step[(t, d)]
                    if n is not None and n not in dist and n not in blocked:
                        dist[n] = dd
                        nxt.append(n)
            frontier = nxt
        return dist

    def in_window(self, head, tile, r=3):
        """`tile` inside the 7x7 (r=3) window centred on `head` (torus)."""
        dx = (tile[0] - head[0]) % self.W
        dy = (tile[1] - head[1]) % self.H
        return min(dx, self.W - dx) <= r and min(dy, self.H - dy) <= r


# ---- replaying the event stream ------------------------------------------------------------------

class State:
    def __init__(self, gmap):
        self.g = gmap
        self.body, self.team, self.alive, self.born, self.parent = {}, {}, set(), {}, {}
        for i, (t, b) in enumerate(gmap.dragons):
            self.body[i] = collections.deque(b)
            self.team[i] = t
            self.alive.add(i)
            self.born[i] = -1
        self.pearls = set()
        self.beds = set()          # inferred: tiles where a pearl spawned at a round start
        self.round = -1

    def length(self, i):
        return len(self.body[i])

    def head(self, i):
        return self.body[i][0]

    def occupied(self):
        occ = {}
        for i in self.alive:
            for t in self.body[i]:
                occ[t] = i
        return occ

    def standing(self, t):
        ls = [len(self.body[i]) for i in self.alive if self.team[i] == t]
        return dict(count=len(ls), longest=max(ls, default=0), total=sum(ls))


def walk(d):
    """Replay a decoded match. Yields (kind, state, info):
      ('pre',  st, id)            before dragon `id` acts (state as it saw it)
      ('turn', st, rec)           after its turn; rec: round,id,team,action,len_before,head_before,
                                  path (head tiles entered), ate, died (reason or None), len_after,
                                  killed (ids it killed head to head)
      ('death', st, (round, id, team, reason, length, detail))   just before the dragon is removed
      ('split', st, (round, parent, child, team, parent_len_before, child_len))
      ('round_end', st, round)
    """
    g = GameMap(d["map"])
    st = State(g)
    cur = None
    rec = None
    in_spawn = False

    def finish():
        nonlocal rec
        if rec is not None:
            i = rec["id"]
            rec["len_after"] = len(st.body[i]) if rec["died"] is None else 0
            out = rec
            rec = None
            return out
        return None

    for ev in d["events"]:
        k = ev[0]
        if k in ("roundStart", "turnStart"):
            r = finish()
            if r:
                yield ("turn", st, r)
        if k == "roundStart":
            if st.round >= 0:
                yield ("round_end", st, st.round)
            st.round = ev[1]
            in_spawn = True
        elif k == "turnStart":
            in_spawn = False
            cur = ev[1]
            yield ("pre", st, cur)
            rec = dict(round=st.round, id=cur, team=st.team[cur], action=None, len_before=len(st.body[cur]),
                       head_before=st.body[cur][0], path=[], ate=0, died=None, killed=[])
        elif k == "dragonAction":
            if rec is not None:
                rec["action"] = ev[2]
                rec["tle"] = ev[3]
        elif k == "tileChange":
            tile, has = ev[1], ev[2]
            if has:
                st.pearls.add(tile)
                if in_spawn:
                    st.beds.add(tile)
            else:
                st.pearls.discard(tile)
                if rec is not None:
                    rec["ate"] += 1
        elif k == "dragonUpdate":
            i, facing, head, tail = ev[1], ev[2], ev[3], ev[4]
            if st.round < 0 or rec is None or i != rec["id"]:
                continue
            b = st.body[i]
            act = rec["action"]
            if act and act[0] == "move":
                t = b[0]
                path = []
                for s in act[1]:
                    t = g.step(t, s)
                    if t is None:
                        break
                    path.append(t)
                if not path or path[-1] != head:
                    path = [head]
                for p in path:
                    b.appendleft(p)
                rec["path"] = path
                guard = 0
                while b and b[-1] != tail and guard < 1000:
                    b.pop()
                    guard += 1
        elif k == "dragonSplit":
            p, c, t = ev[1], ev[2], ev[3]
            before = len(st.body[p])
            st.body[p] = collections.deque(ev[5])
            st.body[c] = collections.deque(ev[6])
            st.team[c] = t
            st.alive.add(c)
            st.born[c] = st.round
            st.parent[c] = p
            yield ("split", st, (st.round, p, c, t, before, len(ev[6])))
        elif k == "dragonDeath":
            i, reason = ev[1], ev[2]
            detail = ""
            act = rec["action"] if rec else None
            if reason == "head_to_head":
                if rec and i == rec["id"]:
                    detail = "mover"
                elif rec:
                    detail = "hit_by_" + ("ally" if st.team[rec["id"]] == st.team[i] else "enemy")
                    if act and act[0] == "move" and len(act[1]) > 1:
                        detail += "_lunge"
                    rec["killed"].append(i)
            elif reason == "other_body" and rec and i == rec["id"] and act and act[0] == "move":
                # the mover died on the step that hit a body: find who lies there
                t = st.body[i][0]
                for s in act[1]:
                    t2 = g.step(t, s)
                    if t2 is None:
                        break
                    t = t2
                    occ = next((j for j in st.alive if j != i and t in st.body[j]), None)
                    if occ is not None:
                        detail = "into_" + ("ally" if st.team[occ] == st.team[i] else "enemy")
                        break
            elif reason == "no_valid_action":
                detail = ("split_fail" if act and act[0] == "split" else
                          "resign" if (act is None or act[0] == "suicide") else "bad_" + act[0])
            yield ("death", st, (st.round, i, st.team[i], reason, len(st.body[i]), detail))
            if rec is not None and i == rec["id"]:
                rec["died"] = reason
            st.alive.discard(i)
    r = finish()
    if r:
        yield ("turn", st, r)
    yield ("round_end", st, st.round)


def our_team(d):
    """The side pinging 0xA7-topped sonar words (our gather-split-trade line), or None."""
    cnt = collections.Counter()
    g = GameMap(d["map"])
    team = {i: t for i, (t, _) in enumerate(g.dragons)}
    for ev in d["events"]:
        if ev[0] == "dragonSplit":
            team[ev[2]] = ev[3]
        elif ev[0] == "sonarPing" and (ev[3] >> 56) == 0xA7:
            cnt[team.get(ev[1])] += 1
    return cnt.most_common(1)[0][0] if cnt else None


def summary(d):
    g = GameMap(d["map"])
    res = d["result"]
    ours = our_team(d)
    out = dict(map=g.name, size=f"{g.W}x{g.H}", symmetry=g.symmetry, kelp=g.kelp_count, portals=g.portal_count,
               botA=d["botA"], botB=d["botB"], ours=ours, result=res, snaps={}, deaths={"A": collections.Counter(),
               "B": collections.Counter()}, splits={"A": 0, "B": 0}, eaten={"A": 0, "B": 0}, moves={"A": 0, "B": 0})
    for kind, st, info in walk(d):
        if kind == "round_end":
            if info in (50, 150, 300, 400, 450, 499):
                out["snaps"][info] = {t: st.standing(t) for t in "AB"}
            out["last_round"] = info
            out["final"] = {t: st.standing(t) for t in "AB"}
        elif kind == "death":
            out["deaths"][info[2]][info[3] + (":" + info[5] if info[5] else "")] += 1
        elif kind == "split":
            out["splits"][info[3]] += 1
        elif kind == "turn":
            out["eaten"][info["team"]] += info["ate"]
            if info["action"] and info["action"][0] == "move":
                out["moves"][info["team"]] += 1
    return out


def main():
    import argparse
    import json
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("replay")
    ap.add_argument("--events", action="store_true", help="print every event")
    ap.add_argument("--json", help="write the decoded replay (events included) to this JSON file")
    a = ap.parse_args()
    d = load(a.replay)
    if a.events:
        for e in d["events"]:
            print(*e)
        return
    if a.json:
        json.dump(d, open(a.json, "w"), default=str)
    s = summary(d)
    res = s["result"]
    print(f"{s['map']} {s['size']} symmetry={s['symmetry']} kelp={s['kelp']} portals={s['portals']}")
    print(f"botA={s['botA']!r} botB={s['botB']!r} ours(0xA7 sonar)={s['ours']}")
    print(f"winner={res['winner']} by {res['endReason']} after round {s.get('last_round')}: A {res['A']}  B {res['B']}")
    ok = s.get("final") == {"A": res["A"], "B": res["B"]}
    print(f"replayed standings match the recorded result: {ok}")
    for r, v in s["snaps"].items():
        print(f"  r{r:3d}  A {v['A']['count']:3d} dragons longest {v['A']['longest']:3d} total {v['A']['total']:4d}   "
              f"B {v['B']['count']:3d} dragons longest {v['B']['longest']:3d} total {v['B']['total']:4d}")
    for t in "AB":
        print(f"  {t}: moves {s['moves'][t]}, pearls eaten {s['eaten'][t]}, splits {s['splits'][t]}, deaths {dict(s['deaths'][t])}")


