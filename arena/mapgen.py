#!/usr/bin/env python3
"""mapgen.py: a procedural, symmetric map generator for UNSW Battlecode "dragons"
(unswbc 1.2.x .map format). One self-contained file, Python 3 standard library only.

Families (deliberately different structure; see README.md next to this file):
  open            few walls, beds spread evenly, no central cluster
  rich-near-home  most beds near each side's spawns, few in between
  maze            corridors: spanning-tree mazes (1 or 2 wide), stripes, partial mazes
  rooms           rooms joined by narrow doors and/or portals only
  sealed-spawn    queens start in small pens with one exit or none; the rest free
  ring            beds on a ring; empty or walled centre
  off-axis        beds away from the line between the queens
  sparse          a handful of beds with long respawn gaps
  crowded         6-10 starters per side on a small board
  anti-centre     punishes rushing the board centre: walled centre, centre trap, food on
                  edges/seams, portal shortcuts, spawns meeting across the wrap seam
  mixed           random combinations of the above

Usage:
  mapgen.py --seed S -o FILE.map                    drop-in for the arena's worldgen.py: one map,
                                                    the family picked by the seed (S mod 11)
  mapgen.py --family F --seed S -o FILE.map         one map of family F
  mapgen.py --family F --seed S --out DIR [--count N]   N maps of family F (or `all`) into DIR
  mapgen.py --describe DIR_OR_FILE [...]            per-map features, one line each
  mapgen.py --list                                  the families
  mapgen.py --show FILE.map                         an ASCII picture ('#' kelp, '@' portal, 'o' bed)

Deterministic: the same family, seed and index always give the same map (str-seeded
random.Random is stable across runs and platforms). Never reads the environment.
"""
import argparse
import math
import os
import random
import sys
from collections import deque

# Engine directions: 0 N, 1 E, 2 S, 3 W (dragons/core.hpp kDx/kDy).
DX = (0, 1, 0, -1)
DY = (-1, 0, 1, 0)
OPP = (2, 3, 0, 1)

FAMILIES = ["open", "rich-near-home", "maze", "rooms", "sealed-spawn", "ring", "off-axis",
            "sparse", "crowded", "anti-centre", "mixed"]
ALIASES = {"corridor": "maze", "islands": "rooms", "donut": "ring", "anti-center": "anti-centre"}


class Retry(Exception):
    """This attempt can't satisfy its constraints; the caller retries with a new sub-seed."""


# --------------------------------------------------------------------------------------
# The board being built. Tiles are (x, y); edges are ('H', x, y) = north side of tile
# (x, y) and ('V', x, y) = west side of tile (x, y), as the engine indexes them.
# Every mutation is applied to an object and its mirror, so the map stays symmetric.
# --------------------------------------------------------------------------------------
class Board:
    def __init__(self, w, h, sym):
        self.w, self.h, self.sym = w, h, sym
        self.kelp = set()
        self.portal = {}            # edge -> partner edge
        self.pairs = []             # [(e1, e2)] in creation order
        self.beds = {}              # tile -> (lo, hi)
        self.dragons = []           # [(team, [tiles head first])]
        self.occ = {}               # tile -> dragon index
        self.protected = set()      # edges repair may not open
        self.nobed = set()          # tiles no bed may go on
        self.reserved = set()       # tiles no (non-queen) dragon may start on
        self.sealed_ok = set()      # tiles of deliberately sealed pens
        self.unit_limit = None
        self.notes = []

    # geometry ------------------------------------------------------------------------
    def wrap(self, x, y):
        return (x % self.w, y % self.h)

    def mirror(self, t):
        x, y = t
        if self.sym == "x":
            return (x, self.h - 1 - y)
        if self.sym == "y":
            return (self.w - 1 - x, y)
        return (self.w - 1 - x, self.h - 1 - y)

    def nb(self, t, d):
        return self.wrap(t[0] + DX[d], t[1] + DY[d])

    def edge(self, t, d):
        x, y = t
        if d == 0:
            return ("H", x, y)
        if d == 2:
            return ("H", x, (y + 1) % self.h)
        if d == 3:
            return ("V", x, y)
        return ("V", (x + 1) % self.w, y)

    def edge_tiles(self, e):
        o, x, y = e
        if o == "H":
            return (x, (y - 1) % self.h), (x, y)
        return ((x - 1) % self.w, y), (x, y)

    def medge(self, e):
        a, b = self.edge_tiles(e)
        ma, mb = self.mirror(a), self.mirror(b)
        for d in range(4):
            if self.nb(ma, d) == mb:
                return self.edge(ma, d)
        raise AssertionError("mirror of an edge is not an edge")

    def tdist(self, a, b):
        dx = abs(a[0] - b[0]); dy = abs(a[1] - b[1])
        return min(dx, self.w - dx) + min(dy, self.h - dy)

    def tiles(self):
        return [(x, y) for y in range(self.h) for x in range(self.w)]

    def centre_box(self, fx, fy):
        """Tiles within fx*w, fy*h (half-widths) of the geometric board centre."""
        cx, cy = (self.w - 1) / 2, (self.h - 1) / 2
        return {(x, y) for (x, y) in self.tiles()
                if abs(x - cx) <= fx * self.w and abs(y - cy) <= fy * self.h}

    # walls and portals -----------------------------------------------------------------
    def set_kelp(self, e, on=True):
        for f in {e, self.medge(e)}:
            if on:
                if f not in self.portal:
                    self.kelp.add(f)
            elif f not in self.protected:
                self.kelp.discard(f)

    def wall_tile_side(self, t, d, on=True):
        self.set_kelp(self.edge(t, d), on)

    def add_portal(self, e1, e2):
        if e1[0] != e2[0] or e1 == e2:
            return False
        m1, m2 = self.medge(e1), self.medge(e2)
        ends = {e1, e2, m1, m2}
        if any(e in self.portal or e in self.protected for e in ends):
            return False
        if {m1, m2} == {e1, e2}:
            groups = [(e1, e2)]
        elif len(ends) == 4:
            groups = [(e1, e2), (m1, m2)]
        else:
            return False
        for a, b in groups:
            self.kelp.discard(a); self.kelp.discard(b)
            self.portal[a] = b; self.portal[b] = a
            self.pairs.append((a, b))
        return True

    def step(self, t, d):
        e = self.edge(t, d)
        if e in self.kelp:
            return None
        p = self.portal.get(e)
        if p is None:
            return self.nb(t, d)
        o, x, y = p
        if o == "H":
            return (x, y) if d == 2 else (x, (y - 1) % self.h)
        return (x, y) if d == 1 else ((x - 1) % self.w, y)

    def plain_open(self, t, d):
        e = self.edge(t, d)
        return e not in self.kelp and e not in self.portal

    # beds ------------------------------------------------------------------------------
    def add_bed(self, t, gaps):
        m = self.mirror(t)
        if t in self.occ or m in self.occ or t in self.nobed or m in self.nobed:
            return False
        if t in self.beds:
            return False
        self.beds[t] = gaps
        self.beds[m] = gaps
        return True

    # search --------------------------------------------------------------------------
    def bfs(self, sources, blocked=()):
        dist = {}
        q = deque()
        for s in sources:
            if s not in dist:
                dist[s] = 0
                q.append(s)
        while q:
            t = q.popleft()
            for d in range(4):
                u = self.step(t, d)
                if u is not None and u not in dist and u not in blocked:
                    dist[u] = dist[t] + 1
                    q.append(u)
        return dist

    def components(self):
        comp = {}
        n = 0
        for t in self.tiles():
            if t in comp:
                continue
            for u in self.bfs([t]):
                comp[u] = n
            n += 1
        return comp, n

    def repair(self, rng):
        """Open kelp (with its mirror) until every component holding a bed or a dragon is
        joined to the main one, except deliberately sealed pens."""
        for _ in range(4 * self.w * self.h):
            comp, n = self.components()
            relevant = {}
            for t in list(self.beds) + list(self.occ):
                if t in self.sealed_ok:
                    continue
                relevant[comp[t]] = relevant.get(comp[t], 0) + 1
            if len(relevant) <= 1:
                return
            sizes = {}
            for t, c in comp.items():
                sizes[c] = sizes.get(c, 0) + 1
            main = max(relevant, key=lambda c: (relevant[c], sizes[c]))
            # an edge from a relevant non-main component to anywhere else
            target = rng.choice(sorted(c for c in relevant if c != main))
            cands = []
            for e in self.kelp:
                if e in self.protected:
                    continue
                a, b = self.edge_tiles(e)
                if a in self.sealed_ok or b in self.sealed_ok:
                    continue
                ca, cb = comp[a], comp[b]
                if ca != cb and target in (ca, cb):
                    cands.append(e)
            if not cands:
                raise Retry("repair: no kelp to open")
            cands.sort()
            # prefer an opening into the main component
            into_main = [e for e in cands if main in (comp[self.edge_tiles(e)[0]], comp[self.edge_tiles(e)[1]])]
            self.set_kelp(rng.choice(into_main or cands), False)
        raise Retry("repair did not converge")

    # dragons ---------------------------------------------------------------------------
    def place_pair(self, rng, length, head_ok, min_sep, straight=0.6, tries=400, inside=None,
                   mirror_sep=None):
        """Place a team-0 dragon and its mirror (team 1). Returns the team-0 body or None."""
        mirror_sep = min_sep if mirror_sep is None else mirror_sep
        cand = [t for t in (inside if inside is not None else self.tiles()) if head_ok(t)]
        if not cand:
            return None
        for _ in range(tries):
            head = rng.choice(cand)
            body = self._walk(rng, head, length, straight, inside)
            if body is None:
                continue
            mbody = [self.mirror(t) for t in body]
            if set(mbody) & set(body):
                continue
            if any(t in self.occ for t in mbody):
                continue
            # the head must have a free first step that isn't its own body
            if not any(self.step(body[0], d) is not None and self.step(body[0], d) not in self.occ
                       and self.step(body[0], d) not in body and self.step(body[0], d) not in mbody
                       for d in range(4)):
                continue
            # keep enemies apart: distance to the mirror and to every enemy segment
            if min(self.tdist(a, b) for a in body for b in mbody) < mirror_sep:
                continue
            enemy = [t for t, i in self.occ.items() if self.dragons[i][0] == 1]
            if enemy and min(self.tdist(a, b) for a in body for b in enemy) < min_sep:
                continue
            i = len(self.dragons)
            self.dragons.append((0, body))
            self.dragons.append((1, mbody))
            for t in body:
                self.occ[t] = i
            for t in mbody:
                self.occ[t] = i + 1
            return body
        return None

    def _walk(self, rng, head, length, straight, inside):
        allowed = None if inside is None else set(inside)

        def ok(t, body):
            return (t not in self.occ and t not in body and self.mirror(t) != t
                    and self.mirror(t) not in body and (allowed is not None or t not in self.reserved)
                    and (allowed is None or t in allowed))
        if not ok(head, []):
            return None
        body = [head]
        last = None
        for _ in range(length - 1):
            t = body[-1]
            dirs = list(range(4))
            rng.shuffle(dirs)
            if last is not None and rng.random() < straight:
                dirs.remove(last); dirs.insert(0, last)
            for d in dirs:
                u = self.nb(t, d)
                if self.plain_open(t, d) and ok(u, body):
                    body.append(u); last = d
                    break
            else:
                return None
        return body


# --------------------------------------------------------------------------------------
# Parameter sampling shared by the families
# --------------------------------------------------------------------------------------
def sample_size(rng, lo=11, hi=64, square_p=0.4):
    def side():
        # bias towards mid sizes, keep the tails
        return int(round(lo + (hi - lo) * (rng.random() ** 1.2)))
    w = side()
    h = w if rng.random() < square_p else side()
    return w, h


def sample_sym(rng, w, h):
    opts = ["xy", "xy", "x", "y"]
    s = rng.choice(opts)
    # the mirrored dimension must leave room for two sides
    if s == "x" and h < 14:
        s = "xy"
    if s == "y" and w < 14:
        s = "xy"
    return s


def sample_starters(rng, lo=1, hi=10):
    weights = {1: 10, 2: 14, 3: 16, 4: 16, 5: 12, 6: 10, 7: 8, 8: 6, 9: 4, 10: 4}
    keys = [k for k in weights if lo <= k <= hi]
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


def sample_queen_len(rng):
    return rng.choice([2, 3, 3, 4, 4, 4, 5, 6, 8, 10, 14])


def sample_len(rng):
    return rng.choice([2, 2, 3, 3, 3, 4, 4, 5, 6, 8])


GAP_STYLES = ["fast", "medium", "slow", "wide", "mixed"]


def gap_sampler(rng, style=None):
    style = style or rng.choice(GAP_STYLES)

    def one(st):
        if st == "fast":
            lo = rng.randint(1, 6); return (lo, lo + rng.randint(4, 30))
        if st == "medium":
            lo = rng.randint(1, 25); return (lo, lo + rng.randint(20, 120))
        if st == "slow":
            lo = rng.randint(30, 150); return (lo, lo + rng.randint(100, 450))
        if st == "wide":
            return (1, rng.choice([100, 200, 500, 1000, 2000, 3849]))
        raise ValueError(st)
    if style == "mixed":
        palette = [one(rng.choice(["fast", "medium", "slow", "wide"])) for _ in range(rng.randint(2, 4))]
        return style, lambda: rng.choice(palette)
    fixed = one(style)
    if rng.random() < 0.5:      # one value for the whole map, as most contest maps do
        return style, lambda: fixed
    return style, lambda: one(style)


# --------------------------------------------------------------------------------------
# Terrain primitives
# --------------------------------------------------------------------------------------
def random_walls(b, rng, density, maxlen=6):
    """Straight kelp segments until about `density` of all edges are kelp."""
    target = int(density * 2 * b.w * b.h)
    guard = 0
    while len(b.kelp) < target and guard < 20000:
        guard += 1
        t = (rng.randrange(b.w), rng.randrange(b.h))
        d = rng.randrange(4)                    # the side of the tile the wall runs on
        run = (d + 1) % 4 if rng.random() < 0.5 else (d + 3) % 4
        n = rng.randint(1, maxlen)
        for _ in range(n):
            b.wall_tile_side(t, d)
            t = b.nb(t, run)


def maze(b, rng, region=None, cell=1, braid=0.2):
    """A spanning-tree maze over `region` (all tiles by default), corridors `cell` wide,
    with a `braid` share of its walls knocked out to make loops."""
    tiles = set(region) if region is not None else set(b.tiles())

    def block(t):
        return (t[0] // cell, t[1] // cell)
    blocks = {}
    for t in tiles:
        blocks.setdefault(block(t), []).append(t)
    # adjacency between blocks through region tiles
    adj = {k: set() for k in blocks}
    for t in tiles:
        for d in range(4):
            u = b.nb(t, d)
            if u in tiles and block(u) != block(t):
                adj[block(t)].add(block(u))
    start = rng.choice(sorted(blocks))
    seen = {start}
    stack = [start]
    tree = set()
    while stack:
        k = stack[-1]
        nxt = [m for m in sorted(adj[k]) if m not in seen]
        if not nxt:
            stack.pop(); continue
        m = rng.choice(nxt)
        seen.add(m); tree.add(frozenset((k, m))); stack.append(m)
    walls = set()
    for t in tiles:
        for d in (1, 2):
            u = b.nb(t, d)
            if u not in tiles:
                continue
            bt, bu = block(t), block(u)
            if bt != bu and frozenset((bt, bu)) not in tree:
                walls.add(b.edge(t, d))
    for e in sorted(walls):
        if rng.random() >= braid:
            b.set_kelp(e)
    connect(b, rng, tiles)


def connect(b, rng, tiles):
    """Open kelp inside `tiles` (with its mirror) until they are one component through
    paths inside `tiles`. A maze's walls are mirrored too, and a spanning tree plus its
    mirror image seals pockets off. Kruskal over the walls in random order: a wall is
    opened only when it joins two separate pockets."""
    tiles = set(tiles)
    parent = {t: t for t in tiles}

    def find(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    def union(a, c):
        parent[find(a)] = find(c)
    walls = []
    for t in sorted(tiles):
        for d in (1, 2):
            u = b.nb(t, d)
            if u not in tiles:
                continue
            e = b.edge(t, d)
            if e in b.kelp:
                walls.append(e)
            elif e not in b.portal:
                union(t, u)
    rng.shuffle(walls)
    for e in walls:
        a, c = b.edge_tiles(e)
        if e in b.protected or find(a) == find(c):
            continue
        b.set_kelp(e, False)
        for f in (e, b.medge(e)):
            x, y = b.edge_tiles(f)
            if f not in b.kelp and x in tiles and y in tiles:
                union(x, y)


def box(b, x0, y0, bw, bh, gaps=(), protect=False):
    """Kelp around the rectangle of tiles [x0, x0+bw) x [y0, y0+bh), leaving the listed
    perimeter edges open. Returns the perimeter edges."""
    per = []
    for i in range(bw):
        per.append(b.edge(b.wrap(x0 + i, y0), 0))
        per.append(b.edge(b.wrap(x0 + i, y0 + bh - 1), 2))
    for j in range(bh):
        per.append(b.edge(b.wrap(x0, y0 + j), 3))
        per.append(b.edge(b.wrap(x0 + bw - 1, y0 + j), 1))
    for e in per:
        if e in gaps:
            continue
        b.set_kelp(e)
        if protect:
            b.protected.add(e); b.protected.add(b.medge(e))
    return per


def rect_tiles(b, x0, y0, bw, bh):
    return [b.wrap(x0 + i, y0 + j) for j in range(bh) for i in range(bw)]


def random_portals(b, rng, k, min_far=None):
    min_far = min_far if min_far is not None else max(4, min(b.w, b.h) // 3)
    made = 0
    for _ in range(200 * max(1, k)):
        if made >= k:
            break
        o = rng.choice("HV")
        e1 = (o, rng.randrange(b.w), rng.randrange(b.h))
        e2 = (o, rng.randrange(b.w), rng.randrange(b.h))
        if b.tdist(b.edge_tiles(e1)[1], b.edge_tiles(e2)[1]) < min_far:
            continue
        if any(t in b.occ or t in b.reserved or b.mirror(t) in b.reserved
               for e in (e1, e2) for t in b.edge_tiles(e)):
            continue
        if b.add_portal(e1, e2):
            made += 1
    return made


def portal_between(b, rng, area_a, area_b, tries=200):
    """A portal pair with one end on an edge inside area_a and the other inside area_b."""
    sa, sb = set(area_a), set(area_b)
    la, lb = sorted(sa), sorted(sb)
    for _ in range(tries):
        o = rng.choice("HV")
        d = 2 if o == "H" else 1
        ta, tb = rng.choice(la), rng.choice(lb)
        if b.nb(ta, d) not in sa or b.nb(tb, d) not in sb:
            continue
        e1, e2 = b.edge(ta, d), b.edge(tb, d)
        if any(t in b.occ or t in b.reserved or b.mirror(t) in b.reserved
               for e in (e1, e2) for t in b.edge_tiles(e)):
            continue
        if b.add_portal(e1, e2):
            return True
    return False


# --------------------------------------------------------------------------------------
# Bed layouts
# --------------------------------------------------------------------------------------
def beds_even(b, rng, density, gaps, avoid=()):
    """Jittered-grid beds: one bed per cell, cells sized for the density, so there is no
    cluster anywhere (the centre included)."""
    avoid = set(avoid)
    # every bed also places its mirror, so each cell needs only half the density; a whole
    # cell size can't hit every density, so cells are skipped at random to make up the rest
    half = max(density, 1e-3) / 2
    cellsz = max(2, int(math.sqrt(1.0 / half)))
    keep = min(1.0, half * cellsz * cellsz)
    placed = 0
    for cy in range(0, b.h, cellsz):
        for cx in range(0, b.w, cellsz):
            if rng.random() >= keep:
                continue
            for _ in range(6):
                t = (min(b.w - 1, cx + rng.randrange(cellsz)), min(b.h - 1, cy + rng.randrange(cellsz)))
                if t in avoid or t in b.beds:
                    continue
                if b.add_bed(t, gaps()):
                    placed += 1
                    break
    return placed


def beds_where(b, rng, count, pred, gaps, tries_per=40):
    """`count` beds (pairs counted twice) on random tiles satisfying pred."""
    cand = [t for t in b.tiles() if pred(t)]
    if not cand:
        return 0
    start = len(b.beds)
    for _ in range(count * tries_per):
        if len(b.beds) - start >= count:
            break
        b.add_bed(rng.choice(cand), gaps())
    return len(b.beds) - start


def beds_cluster(b, rng, centre, radius, count, gaps):
    pts = [t for t in b.tiles() if b.tdist(t, centre) <= radius]
    rng.shuffle(pts)
    n = 0
    for t in pts:
        if n >= count:
            break
        if b.add_bed(t, gaps()):
            n += 2
    return n


def bed_count(b, density):
    return max(2, int(density * b.w * b.h))


# --------------------------------------------------------------------------------------
# Dragon placement shared by the families
# --------------------------------------------------------------------------------------
def default_sep(b):
    return max(4, min(b.w, b.h) // 4)


def place_team(b, rng, n, qlen, lens, queen_ok=None, other_ok=None, qsep=None, sep=3,
               queen_inside=None):
    qsep = qsep if qsep is not None else default_sep(b)
    queen_ok = queen_ok or (lambda t: True)
    q = b.place_pair(rng, qlen, queen_ok, sep, inside=queen_inside, mirror_sep=qsep)
    if q is None:
        raise Retry("queen placement")
    other_ok = other_ok or (lambda t: True)
    for i in range(n - 1):
        body = b.place_pair(rng, lens[i], other_ok, sep, mirror_sep=max(sep, 3))
        if body is None:
            body = b.place_pair(rng, 2, lambda t: True, 2, mirror_sep=2)
            if body is None:
                b.notes.append("dropped-starter")
    return q


def starters(rng, lo=1, hi=10, short=False):
    n = sample_starters(rng, lo, hi)
    qlen = rng.choice([2, 3, 3, 4]) if short else sample_queen_len(rng)
    lens = [rng.choice([2, 2, 3, 3, 4]) if short else sample_len(rng) for _ in range(n - 1)]
    return n, qlen, lens


def team0_heads(b):
    return [body[0] for team, body in b.dragons if team == 0]


# --------------------------------------------------------------------------------------
# Families. Each takes (rng) and returns (Board, variant string).
# --------------------------------------------------------------------------------------
def fam_open(rng):
    w, h = sample_size(rng)
    b = Board(w, h, sample_sym(rng, w, h))
    random_walls(b, rng, rng.uniform(0.0, 0.03), maxlen=3)
    random_portals(b, rng, rng.choice([0, 0, 0, 1, 2]))
    n, ql, ls = starters(rng)
    place_team(b, rng, n, ql, ls)
    _, gaps = gap_sampler(rng)
    beds_even(b, rng, rng.uniform(0.02, 0.35), gaps)
    return b, "even"


def fam_rich_near_home(rng):
    w, h = sample_size(rng, 16, 64)
    b = Board(w, h, sample_sym(rng, w, h))
    random_walls(b, rng, rng.uniform(0.0, 0.10))
    random_portals(b, rng, rng.choice([0, 0, 1, 2, 3]))
    n, ql, ls = starters(rng)
    place_team(b, rng, n, ql, ls, qsep=max(6, min(w, h) // 3))
    b.repair(rng)
    heads0 = [body[0] for team, body in b.dragons if team == 0]
    heads1 = [body[0] for team, body in b.dragons if team == 1]
    d0, d1 = b.bfs(heads0), b.bfs(heads1)
    radius = rng.randint(3, max(4, min(w, h) // 4))
    _, gaps = gap_sampler(rng, rng.choice(["fast", "medium", "medium", "mixed"]))
    home = lambda t: t in d0 and d0[t] <= radius and d0[t] + 3 <= d1.get(t, 10**9)
    total = bed_count(b, rng.uniform(0.04, 0.25))
    beds_where(b, rng, int(total * 0.85), home, gaps)
    mid = lambda t: t in d0 and abs(d0[t] - d1.get(t, 10**9)) <= 2
    beds_where(b, rng, max(1, int(total * 0.08)), mid, gaps)
    return b, f"r{radius}"


def fam_maze(rng):
    w, h = sample_size(rng, 13, 48)
    b = Board(w, h, sample_sym(rng, w, h))
    kind = rng.choice(["maze", "maze", "maze-wide", "stripes", "partial"])
    if kind == "maze":
        maze(b, rng, braid=rng.uniform(0.05, 0.35))
    elif kind == "maze-wide":
        maze(b, rng, cell=2, braid=rng.uniform(0.0, 0.25))
    elif kind == "partial":
        band = rect_tiles(b, 0, 0, w, max(4, h // 2)) if rng.random() < 0.5 else rect_tiles(b, 0, 0, max(4, w // 2), h)
        maze(b, rng, region=band, braid=rng.uniform(0.05, 0.3))
    else:   # stripes: long 1-wide corridors with periodic gaps
        horiz = rng.random() < 0.5
        period = rng.randint(2, 3)
        gap_every = rng.randint(5, 12)
        L, M = (w, h) if horiz else (h, w)
        for j in range(0, M, period):
            off = rng.randrange(gap_every)
            for i in range(L):
                if (i + off) % gap_every == 0:
                    continue
                t = (i, j) if horiz else (j, i)
                b.wall_tile_side(t, 0 if horiz else 3)
    random_portals(b, rng, rng.choice([0, 0, 1, 2, 3, 4]))
    n, ql, ls = starters(rng, 1, 6)
    place_team(b, rng, n, ql, ls)
    b.repair(rng)
    _, gaps = gap_sampler(rng)
    beds_where(b, rng, bed_count(b, rng.uniform(0.02, 0.25)), lambda t: True, gaps)
    return b, kind


def fam_rooms(rng):
    w, h = sample_size(rng, 18, 64)
    b = Board(w, h, sample_sym(rng, w, h))
    nx, ny = rng.randint(2, max(2, w // 7)), rng.randint(2, max(2, h // 7))
    xs = sorted(rng.sample(range(w), nx))
    ys = sorted(rng.sample(range(h), ny))
    for x in xs:                       # wall lines on the west side of column x
        for y in range(h):
            b.set_kelp(("V", x, y))
    for y in ys:
        for x in range(w):
            b.set_kelp(("H", x, y))
    # rooms = components now; join some with narrow doors, some only by portals
    comp, ncomp = b.components()
    rooms = {}
    for t, c in comp.items():
        rooms.setdefault(c, []).append(t)
    keys = sorted(rooms)
    via_portal = rng.uniform(0.0, 0.7)
    doors = 0
    for e in sorted(b.kelp):
        a, c = b.edge_tiles(e)
        if comp[a] != comp[c] and rng.random() < 0.06:
            if rng.random() >= via_portal:
                b.set_kelp(e, False); doors += 1
    nport = 0
    for _ in range(rng.randint(1, min(10, 2 * len(keys)))):
        ra, rb = rng.sample(keys, 2)
        if portal_between(b, rng, rooms[ra], rooms[rb]):
            nport += 1
    random_walls(b, rng, len(b.kelp) / (2 * w * h) + rng.uniform(0, 0.03), maxlen=3)
    n, ql, ls = starters(rng)
    place_team(b, rng, n, ql, ls)
    b.repair(rng)
    _, gaps = gap_sampler(rng)
    beds_where(b, rng, bed_count(b, rng.uniform(0.02, 0.25)), lambda t: True, gaps)
    return b, f"{nx}x{ny}-p{nport}"


def make_pen(b, rng, exits, iw, ih, region_ok):
    """A kelp pen with `exits` (0 or 1) openings around an iw x ih interior. Returns the
    interior tiles, already reserved."""
    for _ in range(300):
        x0, y0 = rng.randrange(b.w), rng.randrange(b.h)
        inner = rect_tiles(b, x0, y0, iw, ih)
        ring = rect_tiles(b, x0 - 1, y0 - 1, iw + 2, ih + 2)
        mring = {b.mirror(t) for t in ring}
        if mring & set(ring) or any(t in b.occ or t in b.reserved for t in ring + list(mring)):
            continue
        if not all(region_ok(t) for t in inner):
            continue
        if any(b.edge(t, d) in b.portal for t in ring for d in range(4)):
            continue
        # clear the interior and the ring around it, then wall the interior
        for t in ring:
            for d in range(4):
                b.set_kelp(b.edge(t, d), False)
        per = box(b, x0, y0, iw, ih, protect=True)
        if exits:
            e = rng.choice(per)
            for f in (e, b.medge(e)):
                b.protected.discard(f)
            b.set_kelp(e, False)
        for t in inner:
            b.reserved.add(t); b.reserved.add(b.mirror(t))
            b.nobed.add(t); b.nobed.add(b.mirror(t))
            if not exits:
                b.sealed_ok.add(t); b.sealed_ok.add(b.mirror(t))
        return inner
    raise Retry("pen")


def fam_sealed_spawn(rng):
    w, h = sample_size(rng, 16, 48)
    b = Board(w, h, sample_sym(rng, w, h))
    random_walls(b, rng, rng.uniform(0.0, 0.08))
    exits = rng.choice([0, 1, 1])
    iw, ih = rng.randint(3, 5), rng.randint(3, 4)
    inner = make_pen(b, rng, exits, iw, ih, lambda t: True)
    random_portals(b, rng, rng.choice([0, 0, 1, 2]))
    n, _, ls = starters(rng, 2, 8)
    perim = 2 * (iw + ih) - 4
    qlen = rng.randint(2, min(5, perim - 3))
    q = b.place_pair(rng, qlen, lambda t: True, 2, inside=inner, mirror_sep=2, straight=0.3)
    if q is None:
        raise Retry("queen in pen")
    for i in range(n - 1):
        if b.place_pair(rng, ls[i], lambda t: t not in b.reserved, 3) is None:
            b.notes.append("dropped-starter")
    b.repair(rng)
    _, gaps = gap_sampler(rng)
    beds_even(b, rng, rng.uniform(0.02, 0.25), gaps)
    return b, f"exit{exits}-{iw}x{ih}"


def fam_ring(rng):
    w, h = sample_size(rng, 16, 64, square_p=0.6)
    b = Board(w, h, sample_sym(rng, w, h))
    cx, cy = (w - 1) / 2, (h - 1) / 2
    rr = rng.uniform(0.28, 0.4)
    thick = rng.uniform(0.03, 0.08)
    centre = rng.choice(["empty", "walled", "walled-doors"])

    def rnorm(t):
        return math.hypot((t[0] - cx) / w, (t[1] - cy) / h)
    inner_r = rr - thick - rng.uniform(0.06, 0.12)
    core = [t for t in b.tiles() if rnorm(t) < inner_r]
    for t in core:
        b.nobed.add(t)
    if centre != "empty" and core:
        cs = set(core)
        per = [b.edge(t, d) for t in core for d in range(4) if b.nb(t, d) not in cs]
        doors = set(rng.sample(per, min(len(per), rng.randint(1, 3)))) if centre == "walled-doors" else set()
        for e in per:
            if e not in doors and b.medge(e) not in doors:
                b.set_kelp(e)
        for t in core:
            b.reserved.add(t)
    random_walls(b, rng, rng.uniform(0.0, 0.04), maxlen=3)
    random_portals(b, rng, rng.choice([0, 0, 1, 2]))
    n, ql, ls = starters(rng)
    place_team(b, rng, n, ql, ls, queen_ok=lambda t: rnorm(t) >= inner_r,
               other_ok=lambda t: rnorm(t) >= inner_r)
    b.repair(rng)
    _, gaps = gap_sampler(rng)
    ring = lambda t: abs(rnorm(t) - rr) <= thick
    beds_where(b, rng, bed_count(b, rng.uniform(0.03, 0.15)), ring, gaps)
    return b, centre


def axis_distance(b, q, mq, t):
    """Distance from t to the shortest-torus segment q -> mq (in unwrapped tiles)."""
    vx = (mq[0] - q[0] + b.w // 2) % b.w - b.w // 2
    vy = (mq[1] - q[1] + b.h // 2) % b.h - b.h // 2
    best = 1e9
    ux0 = (t[0] - q[0]) % b.w
    uy0 = (t[1] - q[1]) % b.h
    for ox in (-b.w, 0, b.w):
        for oy in (-b.h, 0, b.h):
            ux, uy = ux0 + ox, uy0 + oy
            ll = vx * vx + vy * vy
            s = 0 if ll == 0 else max(0.0, min(1.0, (ux * vx + uy * vy) / ll))
            best = min(best, math.hypot(ux - s * vx, uy - s * vy))
    return best


def fam_off_axis(rng):
    w, h = sample_size(rng, 18, 64)
    b = Board(w, h, sample_sym(rng, w, h))
    random_walls(b, rng, rng.uniform(0.0, 0.08))
    random_portals(b, rng, rng.choice([0, 0, 1, 2]))
    n, ql, ls = starters(rng)
    q = place_team(b, rng, n, ql, ls, qsep=max(6, min(w, h) // 3))
    mq = b.mirror(q[0])
    b.repair(rng)
    thr = rng.uniform(0.2, 0.3) * min(w, h)
    _, gaps = gap_sampler(rng)
    placed = beds_where(b, rng, bed_count(b, rng.uniform(0.03, 0.2)),
                        lambda t: axis_distance(b, q[0], mq, t) >= thr, gaps)
    if placed < 2:
        raise Retry("off-axis beds")
    return b, f"thr{thr:.0f}"


def fam_sparse(rng):
    w, h = sample_size(rng)
    b = Board(w, h, sample_sym(rng, w, h))
    random_walls(b, rng, rng.uniform(0.0, 0.10))
    random_portals(b, rng, rng.choice([0, 0, 1, 2]))
    n, ql, ls = starters(rng)
    place_team(b, rng, n, ql, ls)
    b.repair(rng)
    _, gaps = gap_sampler(rng, rng.choice(["slow", "slow", "wide"]))
    k = rng.randint(2, 10)
    beds_where(b, rng, k, lambda t: True, gaps)
    return b, f"k{len(b.beds)}"


def fam_crowded(rng):
    w, h = sample_size(rng, 11, 24)
    b = Board(w, h, sample_sym(rng, w, h))
    random_walls(b, rng, rng.uniform(0.0, 0.06), maxlen=3)
    random_portals(b, rng, rng.choice([0, 0, 1]), min_far=4)
    n, ql, ls = starters(rng, 6, 10, short=True)
    place_team(b, rng, n, ql, ls, qsep=4, sep=2)
    if rng.random() < 0.4:
        b.unit_limit = rng.randint(n + 2, 24)
    b.repair(rng)
    _, gaps = gap_sampler(rng)
    beds_where(b, rng, bed_count(b, rng.uniform(0.03, 0.3)), lambda t: True, gaps)
    return b, f"n{n}"


def fam_anti_centre(rng):
    kind = rng.choice(["walled", "trap", "edges", "portal", "offcentre"])
    w, h = sample_size(rng, 21, 64)
    sym = sample_sym(rng, w, h)
    b = Board(w, h, sym)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    middle = b.centre_box(0.25, 0.25)
    for t in middle:
        b.nobed.add(t)
    _, gaps = gap_sampler(rng, rng.choice(["fast", "medium", "mixed"]))
    n, ql, ls = starters(rng, 1, 8)
    away = lambda t: t not in middle
    if kind == "walled":
        # concentric boxes around the centre, each with one door on alternating sides:
        # reaching the middle takes a long detour, and nothing is there
        rings = rng.randint(2, 3)
        for k in range(rings):
            hw = int(w * (0.08 + 0.07 * k)); hh = int(h * (0.08 + 0.07 * k))
            hw, hh = max(2, hw), max(2, hh)
            x0, y0 = int(cx) - hw, int(cy) - hh
            bw, bh = 2 * hw + 1 + (w + 1) % 2, 2 * hh + 1 + (h + 1) % 2
            per = box(b, x0, y0, bw, bh)
            side = (k + rng.randrange(2)) % 2
            door = b.edge((x0 + (bw - 1) * side, y0 + bh // 2), 3 if side == 0 else 1)
            b.set_kelp(door, False)
            for t in rect_tiles(b, x0, y0, bw, bh):
                b.reserved.add(t)
        place_team(b, rng, n, ql, ls, queen_ok=away, other_ok=away)
        b.repair(rng)
        beds_where(b, rng, bed_count(b, rng.uniform(0.03, 0.08)), away, gaps)
    elif kind == "trap":
        hw, hh = max(3, w // 6), max(3, h // 6)
        x0, y0 = int(cx) - hw, int(cy) - hh
        bw, bh = 2 * hw + 1 + (w + 1) % 2, 2 * hh + 1 + (h + 1) % 2
        region = rect_tiles(b, x0, y0, bw, bh)
        maze(b, rng, region=region, braid=0.0)
        per = box(b, x0, y0, bw, bh)
        door = rng.choice(per)
        b.set_kelp(door, False)
        for t in region:
            b.reserved.add(t)
        random_walls(b, rng, rng.uniform(0.0, 0.03) + len(b.kelp) / (2 * w * h), maxlen=3)
        place_team(b, rng, n, ql, ls, queen_ok=away, other_ok=away)
        b.repair(rng)
        if rng.random() < 0.5:          # a bait bed deep in the trap
            far = b.bfs([b.edge_tiles(door)[0], b.edge_tiles(door)[1]])
            deep = max((t for t in region if t in far), key=lambda t: far[t], default=None)
            if deep is not None:
                b.nobed.discard(deep); b.nobed.discard(b.mirror(deep))
                b.add_bed(deep, (100, 400))
        beds_where(b, rng, bed_count(b, rng.uniform(0.03, 0.08)), away, gaps)
    elif kind == "edges":
        random_walls(b, rng, rng.uniform(0.0, 0.06))
        place_team(b, rng, n, ql, ls, queen_ok=away, other_ok=away)
        b.repair(rng)
        heads = [body[0] for _, body in b.dragons]
        d = b.bfs(heads)
        near = lambda t: d.get(t, 99) <= 4 and away(t)
        seam = lambda t: min(t[0], w - 1 - t[0]) <= 1 or min(t[1], h - 1 - t[1]) <= 1
        total = bed_count(b, rng.uniform(0.04, 0.10))
        beds_where(b, rng, total // 2, near, gaps)
        beds_where(b, rng, total // 2, lambda t: seam(t) and away(t), gaps)
    elif kind == "portal":
        random_walls(b, rng, rng.uniform(0.0, 0.05))
        # food clusters in a far region; portals beside each spawn lead there
        place_team(b, rng, n, ql, ls, queen_ok=away, other_ok=away, qsep=max(6, min(w, h) // 3))
        q = b.dragons[0][1]
        d = b.bfs([q[0]])
        far = [t for t in d if t not in middle and b.mirror(t) not in middle and d[t] >= max(d.values()) * 0.6]
        if not far:
            raise Retry("no far area")
        spot = rng.choice(sorted(far))
        beds_cluster(b, rng, spot, rng.randint(2, 4), bed_count(b, rng.uniform(0.02, 0.05)), gaps)
        near_q = [t for t in b.tiles() if 2 <= b.tdist(t, q[0]) <= 4 and t not in b.occ]
        near_s = [t for t in b.tiles() if b.tdist(t, spot) <= 2 and t not in b.occ]
        made = sum(portal_between(b, rng, near_q, near_s) for _ in range(rng.randint(1, 2)))
        if not made:
            raise Retry("portal shortcut")
        b.repair(rng)
        beds_where(b, rng, bed_count(b, 0.01), away, gaps)
    else:   # offcentre: spawns near the wrap seam, so their meeting point is the seam
        random_walls(b, rng, rng.uniform(0.0, 0.05))
        if sym == "y":
            seam_ok = lambda t: 2 <= t[0] <= max(3, w // 6)
            spot = (0, rng.randrange(h))
        elif sym == "x":
            seam_ok = lambda t: 2 <= t[1] <= max(3, h // 6)
            spot = (rng.randrange(w), 0)
        else:
            seam_ok = lambda t: 2 <= t[0] <= max(3, w // 6) and 2 <= t[1] <= max(3, h // 6)
            spot = (0, 0)
        q = place_team(b, rng, n, ql, ls, queen_ok=seam_ok, other_ok=away, qsep=4)
        if sym != "xy":     # cluster at the seam level of the queen
            spot = (0, q[0][1]) if sym == "y" else (q[0][0], 0)
        b.repair(rng)
        beds_cluster(b, rng, spot, rng.randint(2, 5), bed_count(b, rng.uniform(0.02, 0.05)), gaps)
        beds_where(b, rng, bed_count(b, 0.01), away, gaps)
    return b, kind


def fam_mixed(rng):
    w, h = sample_size(rng)
    b = Board(w, h, sample_sym(rng, w, h))
    terrain = rng.choice(["none", "walls", "maze-part", "rooms", "ring-wall", "dense-walls"])
    beds = rng.choice(["even", "random", "cluster-centre", "home", "off-axis", "sparse", "ring"])
    pen = rng.random() < 0.2 and min(w, h) >= 16
    if terrain == "walls":
        random_walls(b, rng, rng.uniform(0.03, 0.12))
    elif terrain == "dense-walls":
        random_walls(b, rng, rng.uniform(0.15, 0.30), maxlen=4)
    elif terrain == "maze-part":
        cx, cy = rng.randrange(w), rng.randrange(h)
        maze(b, rng, region=rect_tiles(b, cx, cy, max(5, w // 3), max(5, h // 3)),
             braid=rng.uniform(0.0, 0.3))
    elif terrain == "rooms":
        for x in sorted(rng.sample(range(w), rng.randint(1, 3))):
            for y in range(h):
                if rng.random() > 0.12:
                    b.set_kelp(("V", x, y))
    elif terrain == "ring-wall":
        cx, cy = (w - 1) / 2, (h - 1) / 2
        r = rng.uniform(0.15, 0.3)
        core = {t for t in b.tiles() if math.hypot((t[0] - cx) / w, (t[1] - cy) / h) < r}
        for t in core:
            for d in range(4):
                if b.nb(t, d) not in core and rng.random() > 0.1:
                    b.set_kelp(b.edge(t, d))
    random_portals(b, rng, rng.choice([0, 0, 1, 2, 3, 5]))
    inner = None
    if pen:
        inner = make_pen(b, rng, rng.choice([0, 1]), rng.randint(3, 4), 3, lambda t: True)
    crowd = rng.random() < 0.2
    n, ql, ls = starters(rng, 2 if pen else 1, 10, short=crowd)
    if inner is not None:
        q = b.place_pair(rng, rng.randint(2, 4), lambda t: True, 2, inside=inner, mirror_sep=2, straight=0.3)
        if q is None:
            raise Retry("pen queen")
        for i in range(n - 1):
            b.place_pair(rng, ls[i], lambda t: t not in b.reserved, 3)
    else:
        place_team(b, rng, n, ql, ls, qsep=rng.choice([4, default_sep(b), max(6, min(w, h) // 3)]))
    if rng.random() < 0.15:
        b.unit_limit = rng.randint(n + 2, 64)
    b.repair(rng)
    _, gaps = gap_sampler(rng)
    dens = rng.uniform(0.01, 0.4)
    q0 = b.dragons[0][1][0]
    if beds == "even":
        beds_even(b, rng, dens, gaps)
    elif beds == "random":
        beds_where(b, rng, bed_count(b, dens), lambda t: True, gaps)
    elif beds == "cluster-centre":
        beds_cluster(b, rng, (w // 2, h // 2), rng.randint(2, 5), bed_count(b, dens), gaps)
        beds_where(b, rng, bed_count(b, dens / 3), lambda t: True, gaps)
    elif beds == "home":
        d0 = b.bfs(team0_heads(b)); d1 = b.bfs([b.mirror(t) for t in team0_heads(b)])
        beds_where(b, rng, bed_count(b, dens), lambda t: t in d0 and d0[t] + 3 <= d1.get(t, 10**9), gaps)
    elif beds == "off-axis":
        mq = b.mirror(q0)
        thr = 0.22 * min(w, h)
        beds_where(b, rng, bed_count(b, dens), lambda t: axis_distance(b, q0, mq, t) >= thr, gaps)
    elif beds == "sparse":
        _, slow = gap_sampler(rng, "slow")
        beds_where(b, rng, rng.randint(2, 8), lambda t: True, slow)
    else:
        cx, cy = (w - 1) / 2, (h - 1) / 2
        rr = rng.uniform(0.3, 0.42)
        beds_where(b, rng, bed_count(b, dens),
                   lambda t: abs(math.hypot((t[0] - cx) / w, (t[1] - cy) / h) - rr) < 0.05, gaps)
    return b, f"{terrain}+{beds}" + ("+pen" if pen else "") + ("+crowd" if crowd else "")


def fam_trick(rng):
    """Maps that break assumptions a bot may have picked up from the contest maps. Not in the
    default rotation (FAMILIES): asked for by name.
      seam      kelp frames the inside of the board; the food sits on the wrap seam, so the
                way there is across the edge
      split     a full kelp line cuts the board in two halves (one per side); the only way
                across is portals
      honeypot  a dense, slow cluster at the bottom of a 1-wide maze in the middle
      contested no food within 6 steps of either queen; beds only where both queens are about
                equally far (|d0 - d1| <= 1), so feeding means meeting the enemy
      giant     58-64 a side (the contest maximum), few beds, far apart: CPU and search
                depth (bigger boards are legal, 7-256, but the contest maps stop at 64)
      tiny      7-9 a side
    """
    kind = rng.choice(["seam", "split", "honeypot", "contested", "giant", "tiny"])
    if kind == "giant":
        w = h = rng.randint(58, 64)
    elif kind == "tiny":
        w = h = rng.randint(7, 9)
    else:
        w, h = sample_size(rng, 24, 56)
    sym = "xy" if kind in ("tiny", "seam") else sample_sym(rng, w, h)
    if kind == "split":
        sym = rng.choice(["x", "y"])
    b = Board(w, h, sym)
    if kind == "seam":
        # a kelp frame one tile in from every border, with one door per side far from the
        # spawns; the 2-wide band across the wrap (x or y in {0, last}) holds the food
        for x in range(w):
            for y in (1, h - 1):
                b.set_kelp(("H", x, y))
        for y in range(h):
            for x in (1, w - 1):
                b.set_kelp(("V", x, y))
        for e in (("H", w // 2, 1), ("V", 1, h // 2)):
            b.set_kelp(e, False)
        band = lambda t: t[0] in (0, w - 1) or t[1] in (0, h - 1)
        n, ql, ls = starters(rng, 1, 5)
        place_team(b, rng, n, ql, ls, queen_ok=lambda t: not band(t), other_ok=lambda t: not band(t))
        b.repair(rng)
        _, gaps = gap_sampler(rng, "fast")
        beds_where(b, rng, max(8, (w + h)), band, gaps)
    elif kind == "split":
        # the line runs along the mirrored axis's middle, so each team's half is its own;
        # both seams (the middle and the wrap) are walled
        if sym == "x":      # (x, y) -> (x, H-1-y): halves are top and bottom
            for x in range(w):
                b.set_kelp(("H", x, h // 2)); b.set_kelp(("H", x, 0))
            half0 = lambda t: t[1] < h // 2
        else:
            for y in range(h):
                b.set_kelp(("V", w // 2, y)); b.set_kelp(("V", 0, y))
            half0 = lambda t: t[0] < w // 2
        random_walls(b, rng, rng.uniform(0.0, 0.04) + len(b.kelp) / (2 * w * h), maxlen=3)
        n, ql, ls = starters(rng, 1, 5)
        place_team(b, rng, n, ql, ls, queen_ok=half0, other_ok=half0)
        mine = [t for t in b.tiles() if half0(t)]
        theirs = [t for t in b.tiles() if not half0(t)]
        if not sum(portal_between(b, rng, mine, theirs) for _ in range(rng.randint(1, 3))):
            raise Retry("split portals")
        _, gaps = gap_sampler(rng)
        beds_even(b, rng, rng.uniform(0.03, 0.15), gaps)
    elif kind == "honeypot":
        hw, hh = max(3, w // 6), max(3, h // 6)
        cx, cy = (w - 1) // 2, (h - 1) // 2
        x0, y0 = cx - hw, cy - hh
        bw, bh = 2 * hw + 1 + (w + 1) % 2, 2 * hh + 1 + (h + 1) % 2
        region = rect_tiles(b, x0, y0, bw, bh)
        maze(b, rng, region=region, braid=0.0)
        per = box(b, x0, y0, bw, bh)
        door = rng.choice(per)
        b.set_kelp(door, False)
        for t in region:
            b.reserved.add(t)
        n, ql, ls = starters(rng, 1, 6)
        place_team(b, rng, n, ql, ls)
        b.repair(rng)
        far = b.bfs(list(b.edge_tiles(door)))
        deep = sorted((t for t in region if t in far), key=lambda t: -far[t])
        for t in deep[:max(4, len(deep) // 4)]:
            b.add_bed(t, (rng.randint(60, 150), rng.randint(200, 500)))
        _, gaps = gap_sampler(rng, "medium")
        beds_where(b, rng, bed_count(b, 0.02), lambda t: t not in set(region), gaps)
    elif kind == "contested":
        random_walls(b, rng, rng.uniform(0.0, 0.06))
        n, ql, ls = starters(rng)
        place_team(b, rng, n, ql, ls, qsep=max(8, min(w, h) // 3))
        b.repair(rng)
        q0 = b.dragons[0][1][0]
        d0, d1 = b.bfs([q0]), b.bfs([b.mirror(q0)])
        _, gaps = gap_sampler(rng, rng.choice(["fast", "medium"]))
        placed = beds_where(b, rng, bed_count(b, rng.uniform(0.04, 0.12)),
                            lambda t: t in d0 and t in d1 and min(d0[t], d1[t]) > 6
                            and abs(d0[t] - d1[t]) <= 1, gaps)
        if placed < 4:
            raise Retry("contested beds")
    elif kind == "giant":
        random_walls(b, rng, rng.uniform(0.0, 0.05))
        random_portals(b, rng, rng.choice([0, 2, 6]))
        n, ql, ls = starters(rng, 1, 6)
        place_team(b, rng, n, ql, ls, qsep=w // 3)
        b.repair(rng)
        _, gaps = gap_sampler(rng)
        beds_where(b, rng, rng.randint(12, 60), lambda t: True, gaps)
    else:   # tiny
        n, ql, ls = starters(rng, 1, 3, short=True)
        place_team(b, rng, n, ql, ls, qsep=3, sep=2)
        _, gaps = gap_sampler(rng, "fast")
        beds_where(b, rng, rng.randint(2, 8), lambda t: True, gaps)
    return b, kind


GENERATORS = {
    "trick": fam_trick,
    "open": fam_open, "rich-near-home": fam_rich_near_home, "maze": fam_maze, "rooms": fam_rooms,
    "sealed-spawn": fam_sealed_spawn, "ring": fam_ring, "off-axis": fam_off_axis, "sparse": fam_sparse,
    "crowded": fam_crowded, "anti-centre": fam_anti_centre, "mixed": fam_mixed,
}


# --------------------------------------------------------------------------------------
# Validation (mirrors shared/engine/dragons/board.cpp, plus playability rules)
# --------------------------------------------------------------------------------------
def validate(b):
    errs = []
    if not (7 <= b.w <= 256 and 7 <= b.h <= 256):
        errs.append("size")
    if not b.beds:
        errs.append("no beds")
    for t, g in b.beds.items():
        if b.beds.get(b.mirror(t)) != g:
            errs.append(f"bed {t} mirror")
        if not (1 <= g[0] <= g[1]):
            errs.append(f"bed {t} gaps {g}")
    used = set()
    for a, c in b.pairs:
        if a[0] != c[0]:
            errs.append("portal orientation")
        for e in (a, c):
            if e in used or e in b.kelp:
                errs.append(f"portal edge {e}")
            used.add(e)
    teams = [t for t, _ in b.dragons]
    if not teams or teams[0] != 0 or any(teams[i] == teams[i - 1] for i in range(1, len(teams))):
        errs.append("teams must alternate")
    seen = set()
    for i, (team, body) in enumerate(b.dragons):
        if len(body) < 2:
            errs.append(f"dragon {i} short")
        for k, t in enumerate(body):
            if t in seen:
                errs.append(f"dragon {i} overlaps")
            seen.add(t)
            if k and not any(b.nb(t, d) == body[k - 1] and b.plain_open(t, d) for d in range(4)):
                errs.append(f"dragon {i} segment {k} not adjacent")
        if not any(b.step(body[0], d) is not None and b.step(body[0], d) not in seen | set(body)
                   and b.step(body[0], d) not in b.occ for d in range(4)):
            errs.append(f"dragon {i} head boxed in")
        if b.mirror(body[0]) not in b.occ:
            errs.append(f"dragon {i} not mirrored")
    if len(b.dragons) >= 2:
        q0, q1 = b.dragons[0][1][0], b.dragons[1][1][0]
        if q0 not in b.sealed_ok:
            reach = b.bfs([q0])
            if q1 not in reach:
                errs.append("queens can't reach each other")
            if not any(t in reach for t in b.beds):
                errs.append("queen reaches no bed")
            lost = [t for t in b.beds if t not in reach and t not in b.sealed_ok]
            if lost:
                errs.append(f"{len(lost)} beds out of reach")
        for i, (team, body) in enumerate(b.dragons):
            if body[0] in b.sealed_ok:
                continue
            reach = b.bfs([body[0]])
            if not any(t in reach for t in b.beds) and not any(t in reach for t in (q0, q1)):
                errs.append(f"dragon {i} reaches nothing")
    return errs


def build(family, seed, index, max_attempts=60):
    for attempt in range(max_attempts):
        rng = random.Random(f"{family}:{seed}:{index}:{attempt}")
        try:
            b, variant = GENERATORS[family](rng)
            if not b.beds:
                raise Retry("no beds")
            b.repair(rng)           # beds went in after the families' repair: join them too
            errs = validate(b)
            if errs:
                raise Retry("; ".join(errs[:3]))
            return b, variant
        except Retry:
            continue
    raise RuntimeError(f"{family} seed {seed} index {index}: no valid map in {max_attempts} attempts")


def edge_file_index(b, e):
    o, x, y = e
    row = 2 * y if o == "H" else 2 * y + 1
    return row * (b.w + 1) + x


def render(b, name):
    out = [f"MAP {b.w} {b.h}", f"SYMMETRY {b.sym}", f"MAP_NAME {name}"]
    if b.unit_limit:
        out.append(f"UNIT_LIMIT {b.unit_limit}")
    beds = sorted(b.beds.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    out.append(f"TILE_COUNT {len(beds)}")
    out += [f"TILE {x} {y} {lo} {hi}" for (x, y), (lo, hi) in beds]
    edges = [(edge_file_index(b, e), 1, -1) for e in b.kelp]
    for pid, (a, c) in enumerate(b.pairs):
        edges.append((edge_file_index(b, a), 2, pid))
        edges.append((edge_file_index(b, c), 2, pid))
    edges.sort()
    out.append(f"EDGE_COUNT {len(edges)}")
    out += [f"EDGE {i} {k} {p}" for i, k, p in edges]
    out.append(f"DRAGON_COUNT {len(b.dragons)}")
    for team, body in b.dragons:
        out.append(f"DRAGON {team} {len(body)} " + " ".join(f"{x} {y}" for x, y in body))
    out.append("END")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------
# Reading any .map (generated or not) back, for --describe
# --------------------------------------------------------------------------------------
def parse(text):
    b = None
    name = ""
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        k = f[0]
        if k == "END":
            break
        if k == "MAP":
            b = Board(int(f[1]), int(f[2]), "none")
        elif k == "SYMMETRY":
            b.sym = f[1]
        elif k == "MAP_NAME":
            name = " ".join(f[1:])
        elif k == "UNIT_LIMIT":
            b.unit_limit = int(f[1])
        elif k == "TILE":
            x, y, lo, hi = map(int, f[1:5])
            if hi > 0:
                b.beds[(x, y)] = (lo, hi)
        elif k == "EDGE":
            idx, kind, pid = map(int, f[1:4])
            stride = b.w + 1
            col, row = idx % stride, idx // stride
            if col == b.w or row >= 2 * b.h:
                continue
            e = ("H", col, row // 2) if row % 2 == 0 else ("V", col, (row - 1) // 2)
            if kind == 1:
                b.kelp.add(e)
            elif kind == 2:
                b._ends = getattr(b, "_ends", {})
                b._ends.setdefault(pid, []).append(e)
        elif k == "DRAGON":
            team, n = int(f[1]), int(f[2])
            body = [(int(f[3 + 2 * i]), int(f[4 + 2 * i])) for i in range(n)]
            for t in body:
                b.occ[t] = len(b.dragons)
            b.dragons.append((team, body))
    for pid, ends in getattr(b, "_ends", {}).items():
        if len(ends) == 2:
            b.portal[ends[0]] = ends[1]; b.portal[ends[1]] = ends[0]
            b.pairs.append((ends[0], ends[1]))
    if b.sym == "none":
        b.sym = "xy"        # only used for mirror lookups in features; harmless
    return b, name


def pen_exit(b, q, limit=30):
    """True when cutting one open step near q leaves q in a region of <= limit tiles."""
    near = b.bfs([q])
    near = {t for t, v in near.items() if v <= 8}
    for t in sorted(near):
        for d in (1, 2):
            u = b.step(t, d)
            if u is None:
                continue
            seen = {q}
            stack = [q]
            while stack and len(seen) <= limit:
                a = stack.pop()
                for k in range(4):
                    c = b.step(a, k)
                    if c is None or c in seen:
                        continue
                    if (a == t and k == d) or (a == u and c == t and b.step(u, k) == t):
                        continue
                    seen.add(c); stack.append(c)
            if len(seen) <= limit:
                return True
    return False


def features(b, name):
    W, H = b.w, b.h
    n0 = sum(1 for t, _ in b.dragons if t == 0)
    n1 = sum(1 for t, _ in b.dragons if t == 1)
    qlen = len(b.dragons[0][1]) if b.dragons else 0
    lens0 = [len(body) for t, body in b.dragons if t == 0]
    # corridor share: open tiles with kelp on 2+ sides
    open_tiles = corr = 0
    for t in b.tiles():
        k = sum(1 for d in range(4) if b.edge(t, d) in b.kelp)
        if k < 4:
            open_tiles += 1
            corr += k >= 2
    corridor = corr / max(1, open_tiles)
    # queens
    q0 = b.dragons[0][1][0] if b.dragons else None
    q1 = b.dragons[1][1][0] if len(b.dragons) > 1 else None
    seal = "-"
    if q0 is not None:
        reach = b.bfs([q0])
        if q1 not in reach:
            seal = "sealed"
        elif pen_exit(b, q0):
            seal = "pen1"
        else:
            seal = "open"
    # beds: centre share, coverage, home/contested split, axis share
    nb_ = len(b.beds)
    cx, cy = (W - 1) / 2, (H - 1) / 2
    centre = sum(1 for (x, y) in b.beds if abs(x - cx) <= W / 4 and abs(y - cy) <= H / 4)
    centre_share = centre / nb_ if nb_ else 0
    cluster = "CLUSTER" if nb_ >= 4 and centre_share >= 0.45 else "-"
    blocks = {(x * 4 // W, y * 4 // H) for (x, y) in b.beds}
    coverage = len(blocks) / 16
    home = contested = 0
    axis = 0
    if b.dragons:
        h0 = [body[0] for t, body in b.dragons if t == 0]
        h1 = [body[0] for t, body in b.dragons if t == 1]
        d0, d1 = b.bfs(h0), b.bfs(h1)
        for t in b.beds:
            a, c = d0.get(t, 10**6), d1.get(t, 10**6)
            if abs(a - c) <= 2:
                contested += 1
            else:
                home += 1
        if q1 is not None:
            near = 0.15 * min(W, H)
            axis = sum(1 for t in b.beds if axis_distance(b, q0, q1, t) <= near) / max(1, nb_)
        qd = b.bfs([q0]).get(q1, -1)
    else:
        qd = -1
    gaps = [lo + hi for lo, hi in b.beds.values()]
    mean_gap = sum(gaps) / (2 * len(gaps)) if gaps else 0
    return {
        "name": name, "size": f"{W}x{H}", "sym": b.sym, "starters": f"{n0}/{n1}", "qlen": qlen,
        "len": f"{sum(lens0) / max(1, len(lens0)):.1f}", "beds": nb_, "bed%": f"{100 * nb_ / (W * H):.1f}",
        "gap": f"{mean_gap:.0f}", "kelp%": f"{100 * len(b.kelp) / (2 * W * H):.0f}", "portals": len(b.pairs),
        "corridor%": f"{100 * corridor:.0f}", "spawn": seal, "qdist": qd,
        "centre%": f"{100 * centre_share:.0f}", "cluster": cluster, "cover": f"{coverage:.2f}",
        "contested%": f"{100 * contested / max(1, nb_):.0f}", "axis%": f"{100 * axis:.0f}",
        "ulimit": b.unit_limit or 64,
    }


def show(path):
    """ASCII picture: '#' kelp, '@' portal, 'o' bed, 'A'/'B' dragons (queens 'Q'/'q')."""
    with open(path) as fh:
        b, name = parse(fh.read())
    g = [[" "] * (2 * b.w + 1) for _ in range(2 * b.h + 1)]
    for y in range(b.h + 1):
        for x in range(b.w + 1):
            g[2 * y][2 * x] = "+"
    for e in list(b.kelp) + list(b.portal):
        o, x, y = e
        ch = "#" if e in b.kelp else "@"
        if o == "H":
            g[2 * y][2 * x + 1] = ch
            if y == 0:
                g[2 * b.h][2 * x + 1] = ch
        else:
            g[2 * y + 1][2 * x] = ch
            if x == 0:
                g[2 * y + 1][2 * b.w] = ch
    for (x, y) in b.beds:
        g[2 * y + 1][2 * x + 1] = "o"
    for i, (team, body) in enumerate(b.dragons):
        for k, (x, y) in enumerate(body):
            c = ("Q" if team == 0 else "q") if i < 2 else ("A" if team == 0 else "B")
            g[2 * y + 1][2 * x + 1] = c if k == 0 else c.lower() if i >= 2 else ("Q" if team == 0 else "q")
    print(name, f"{b.w}x{b.h}", b.sym)
    for row in g:
        print("".join(row))


def describe(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(os.path.join(p, f) for f in os.listdir(p) if f.endswith(".map"))
        else:
            files.append(p)
    cols = ["file", "size", "sym", "starters", "qlen", "len", "beds", "bed%", "gap", "kelp%", "portals",
            "corridor%", "spawn", "qdist", "centre%", "cluster", "cover", "contested%", "axis%", "ulimit",
            "name"]
    rows = []
    for f in files:
        with open(f) as fh:
            b, name = parse(fh.read())
        r = features(b, name)
        r["file"] = os.path.basename(f)[:-4]
        rows.append(r)
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) if rows else len(c) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--family", help="one of: " + ", ".join(FAMILIES) + ", or all (default: picked by the seed)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("-o", "--output", help="write one map to this file (the arena's worldgen.py interface)")
    ap.add_argument("--out", help="write maps into this directory")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--describe", nargs="+", metavar="DIR_OR_FILE")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", metavar="FILE", help="print an ASCII picture of a map")
    a = ap.parse_args()
    if a.show:
        show(a.show)
        return 0
    if a.list:
        print("\n".join(FAMILIES))
        return 0
    if a.describe:
        describe(a.describe)
        return 0
    fam = ALIASES.get(a.family, a.family) if a.family else None
    if fam is not None and fam != "all" and fam not in GENERATORS:
        ap.error(f"unknown family {a.family}")
    if a.output:
        f = fam if fam not in (None, "all") else FAMILIES[a.seed % len(FAMILIES)]
        b, variant = build(f, a.seed, 0)
        d = os.path.dirname(a.output)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.output, "w") as fh:
            fh.write(render(b, f"gen-{f}-{variant}-s{a.seed}"))
        return 0
    if not a.out:
        ap.error("give -o FILE, --out DIR, --describe or --list")
    os.makedirs(a.out, exist_ok=True)
    for i in range(a.count):
        f = FAMILIES[i % len(FAMILIES)] if fam in (None, "all") else fam
        b, variant = build(f, a.seed, i)
        name = f"gen-{f}-{variant}-s{a.seed}-i{i}"
        path = os.path.join(a.out, f"{f}-s{a.seed}-{i:03d}.map")
        with open(path, "w") as fh:
            fh.write(render(b, name))
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
