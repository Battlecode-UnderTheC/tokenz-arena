// The replay decoder, the board, the game walk and compute(): ports of arena/metrics/state.py and
// arena/metrics/__init__.py compute(), rule for rule (their comments say why each rule is there).
#include "core.h"

#include <zlib.h>

#include <chrono>

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>
#include <sstream>
#include <stdexcept>

namespace am {

PhaseTimes phase_times;
static double now_s() {
  return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

// ---- registry ---------------------------------------------------------------------------------------
static std::vector<std::pair<int, Metric>>& pending() {
  static std::vector<std::pair<int, Metric>> v;
  return v;
}
Register::Register(int order, Metric m) { pending().emplace_back(order, std::move(m)); }
std::vector<Metric>& registry() {
  static std::vector<Metric> reg = [] {
    auto p = pending();
    std::stable_sort(p.begin(), p.end(), [](auto& a, auto& b) { return a.first < b.first; });
    std::vector<Metric> out;
    for (auto& x : p) out.push_back(x.second);
    return out;
  }();
  return reg;
}

double round4(double x) {
  if (!std::isfinite(x)) return x;
  char buf[64];
  std::snprintf(buf, sizeof buf, "%.4f", x);   // correctly rounded, ties to even: CPython's round()
  return std::strtod(buf, nullptr);
}

// ---- reading the file ------------------------------------------------------------------------------
// gunzip into `out` (reused across games: no fresh pages per replay), sized from the trailer
static void inflate_gzip(const std::string& in, std::string& out) {
  size_t isize = 0;
  if (in.size() >= 4) std::memcpy(&isize, in.data() + in.size() - 4, 4);
  out.resize(std::max<size_t>(isize, 1 << 16));
  z_stream z{};
  if (inflateInit2(&z, 16 + MAX_WBITS) != Z_OK) throw std::runtime_error("zlib init");
  z.next_in = (Bytef*)in.data();
  z.avail_in = (uInt)in.size();
  size_t have = 0;
  int r;
  do {
    if (have == out.size()) out.resize(out.size() * 2);
    z.next_out = (Bytef*)out.data() + have;
    z.avail_out = uInt(out.size() - have);
    r = inflate(&z, Z_NO_FLUSH);
    if (r != Z_OK && r != Z_STREAM_END) {
      inflateEnd(&z);
      throw std::runtime_error("not a valid gzip stream");
    }
    have = out.size() - z.avail_out;
  } while (r != Z_STREAM_END);
  inflateEnd(&z);
  out.resize(have);
}

// One packed word: the bytes present in `tag` order, spread to their places. On x86 with BMI2
// (and a fast pdep: Intel, AMD Zen 3+) one pdep does it, else a loop over the present bytes.
#if defined(__x86_64__)
__attribute__((target("bmi2"))) static inline uint64_t spread_pdep(const unsigned char* p, unsigned tag) {
  static const auto MASK = [] {
    std::array<uint64_t, 256> m{};
    for (unsigned t = 0; t < 256; t++)
      for (int b = 0; b < 8; b++)
        if (t & (1u << b)) m[t] |= uint64_t(0xFF) << (8 * b);
    return m;
  }();
  uint64_t v;
  std::memcpy(&v, p, 8);                            // the caller guarantees 8 readable bytes
  return __builtin_ia32_pdep_di(v, MASK[tag]);
}
static bool fast_pdep() {
  static const bool ok = [] {
    if (!__builtin_cpu_supports("bmi2")) return false;
    if (__builtin_cpu_is("intel")) return true;
    // AMD before Zen 3 runs pdep in microcode (hundreds of cycles)
    return __builtin_cpu_is("amd") && !__builtin_cpu_is("znver1") && !__builtin_cpu_is("znver2");
  }();
  return ok;
}
#endif

// state.py unpack: Cap'n Proto packed bytes -> the message's words. One pass to size the output,
// one to fill it.
static void unpack(const char* bytes, size_t n, std::vector<uint64_t>& out) {
  const auto* p = (const unsigned char*)bytes;
  size_t words = 0;
  for (size_t i = 0; i < n;) {
    unsigned tag = p[i++];
    if (tag == 0) {
      if (i >= n) throw std::runtime_error("truncated replay");
      words += size_t(p[i++]) + 1;
    } else if (tag == 0xFF) {
      if (i + 9 > n) throw std::runtime_error("truncated replay");
      size_t cnt = p[i + 8];
      words += 1 + cnt;
      i += 9 + 8 * cnt;
    } else {
      words++;
      i += __builtin_popcount(tag);
    }
  }
  out.resize(words);                               // reused across games: every word is written
  uint64_t* o = out.data();
#if defined(__x86_64__)
  const bool pdep = fast_pdep();
#endif
  for (size_t i = 0; i < n;) {
    unsigned tag = p[i++];
    if (tag == 0) {
      size_t run = size_t(p[i++]) + 1;
      std::memset(o, 0, 8 * run);
      o += run;
    } else if (tag == 0xFF) {
      size_t cnt = p[i + 8];
      if (i + 9 + 8 * cnt > n) throw std::runtime_error("truncated replay");
      std::memcpy(o, p + i, 8);
      o++;
      std::memcpy(o, p + i + 9, 8 * cnt);
      o += cnt;
      i += 9 + 8 * cnt;
    } else {
      unsigned cnt = __builtin_popcount(tag);
      if (i + cnt > n) throw std::runtime_error("truncated replay");
#if defined(__x86_64__)
      if (pdep && i + 8 <= n) {
        *o++ = spread_pdep(p + i, tag);
        i += cnt;
        continue;
      }
#endif
      uint64_t w = 0;
      for (unsigned bits = tag; bits; bits &= bits - 1)   // only the bytes that are there
        w |= uint64_t(p[i++]) << (8 * __builtin_ctz(bits));
      *o++ = w;
    }
  }
}

namespace {
// state.py Msg / Struct
struct Msg {
  std::vector<uint64_t>& words;
  std::vector<size_t> base;            // each segment's first word
  [[noreturn]] static void out_of_range() { throw std::runtime_error("pointer out of the message"); }
  inline uint64_t word(int seg, size_t idx) const {
    if ((size_t)seg >= base.size()) [[unlikely]] out_of_range();
    size_t a = base[seg] + idx;
    if (a >= words.size()) [[unlikely]] out_of_range();
    return words[a];
  }
  struct Ptr {
    int kind = -1, seg = 0;
    size_t target = 0;
    uint64_t w = 0;
  };
  Ptr resolve(int seg, size_t pidx, int depth = 0) const {
    uint64_t w = word(seg, pidx);
    if (w == 0) return {};
    int kind = int(w & 3);
    if (kind == 2) {
      if (depth > 4) throw std::runtime_error("far pointer loop");
      bool dbl = (w >> 2) & 1;
      size_t landing = (w >> 3) & ((1u << 29) - 1);
      int tseg = int(w >> 32);
      if (!dbl) return resolve(tseg, landing, depth + 1);
      uint64_t w1 = word(tseg, landing), tag = word(tseg, landing + 1);
      return {int(tag & 3), int(w1 >> 32), size_t((w1 >> 3) & ((1u << 29) - 1)), tag};
    }
    long long off = (w >> 2) & ((1u << 30) - 1);
    if (off & (1 << 29)) off -= 1 << 30;
    return {kind, seg, size_t((long long)pidx + 1 + off), w};
  }
};

struct Struct {
  const Msg* m = nullptr;
  int seg = 0;
  size_t start = 0;
  unsigned dw = 0, pc = 0;
  explicit operator bool() const { return m != nullptr; }
  uint64_t raw(unsigned byteoff, unsigned size) const {    // fields are aligned to their size
    if (byteoff + size > dw * 8) return 0;
    uint64_t w = m->word(seg, start + byteoff / 8) >> (8 * (byteoff % 8));
    return size == 8 ? w : w & ((uint64_t(1) << (8 * size)) - 1);
  }
  unsigned u16(unsigned o) const { return unsigned(raw(o, 2)); }
  int i32(unsigned o) const { return int32_t(uint32_t(raw(o, 4))); }
  uint32_t u32(unsigned o) const { return uint32_t(raw(o, 4)); }
  uint64_t u64(unsigned o) const { return raw(o, 8); }
  int bit(unsigned n) const { return int((raw(n / 8, 1) >> (n % 8)) & 1); }
  Msg::Ptr ptr(unsigned i) const {
    if (i >= pc) return {};
    return m->resolve(seg, start + dw + i);
  }
  Struct child(unsigned i) const {
    auto r = ptr(i);
    if (r.kind < 0) return {};
    return {m, r.seg, r.target, unsigned((r.w >> 32) & 0xFFFF), unsigned((r.w >> 48) & 0xFFFF)};
  }
  std::string text(unsigned i) const {
    auto r = ptr(i);
    if (r.kind < 0) return "";
    size_t n = r.w >> 35;
    std::string s;
    for (size_t k = 0; k + 1 < n; k++) s.push_back(char((m->word(r.seg, r.target + k / 8) >> (8 * (k % 8))) & 0xFF));
    return s;
  }
  std::vector<unsigned> list_u16(unsigned i) const {
    auto r = ptr(i);
    std::vector<unsigned> out;
    if (r.kind < 0) return out;
    size_t n = r.w >> 35;
    for (size_t k = 0; k < n; k++) out.push_back(unsigned((m->word(r.seg, r.target + k / 4) >> (16 * (k % 4))) & 0xFFFF));
    return out;
  }
  std::vector<Struct> list_struct(unsigned i) const {
    auto r = ptr(i);
    std::vector<Struct> out;
    if (r.kind < 0) return out;
    uint64_t tag = m->word(r.seg, r.target);
    size_t n = (tag >> 2) & ((1u << 30) - 1);
    unsigned dw2 = unsigned((tag >> 32) & 0xFFFF), pc2 = unsigned((tag >> 48) & 0xFFFF);
    for (size_t k = 0; k < n; k++) out.push_back({m, r.seg, r.target + 1 + k * (dw2 + pc2), dw2, pc2});
    return out;
  }
};
}  // namespace

// ---- the board (state.py GameMap) ----------------------------------------------------------------
static int mod(int a, int b) { return ((a % b) + b) % b; }

Map parse_map(const std::string& text) {
  Map g;
  std::map<int, std::pair<int, int>> edges;          // eid -> (type, portal id)
  std::map<int, std::vector<int>> portals;
  std::istringstream lines(text);
  std::string line;
  while (std::getline(lines, line)) {
    std::istringstream ws(line);
    std::vector<std::string> p;
    for (std::string w; ws >> w;) p.push_back(w);
    if (p.empty()) continue;
    const std::string& k = p[0];
    if (k == "MAP") {
      g.W = std::stoi(p.at(1));
      g.H = std::stoi(p.at(2));
    } else if (k == "MAP_NAME") {
      std::string s = line.size() > 9 ? line.substr(9) : "";
      size_t a = s.find_first_not_of(" \t\r\n\f\v"), b = s.find_last_not_of(" \t\r\n\f\v");
      g.name = a == std::string::npos ? "" : s.substr(a, b - a + 1);
    } else if (k == "EDGE") {
      int eid = std::stoi(p.at(1)), typ = std::stoi(p.at(2)), pid = std::stoi(p.at(3));
      edges[eid] = {typ, pid};
      if (typ == 2) portals[pid].push_back(eid);
    } else if (k == "DRAGON" || k == "SNAKE") {
      std::vector<int> v;
      for (size_t i = 1; i < p.size(); i++) v.push_back(std::stoi(p[i]));
      std::vector<int> xy;                         // (x, y) pairs: tiles once W is known
      for (int i = 0; i < v.at(1); i++) {
        xy.push_back(v.at(2 + 2 * i));
        xy.push_back(v.at(3 + 2 * i));
      }
      g.dragons.push_back({"AB"[v.at(0)], xy});
    }
  }
  if (g.W <= 0 || g.H <= 0) throw std::runtime_error("map has no MAP line");
  for (auto& [team, body] : g.dragons) {
    std::vector<int> tiles;
    for (size_t i = 0; i + 1 < body.size(); i += 2) tiles.push_back(body[i + 1] * g.W + body[i]);
    body = tiles;
  }
  std::map<int, int> partner;
  for (auto& [pid, es] : portals)
    if (es.size() == 2) {
      partner[es[0]] = es[1];
      partner[es[1]] = es[0];
    }
  const int W = g.W, H = g.H;
  auto edge_id = [&](int x, int y, int d) {
    if (d == 0) return 2 * y * (W + 1) + x;
    if (d == 2) return 2 * ((y + 1) % H) * (W + 1) + x;
    if (d == 3) return (2 * y + 1) * (W + 1) + x;
    return (2 * y + 1) * (W + 1) + (x + 1) % W;
  };
  static const int DX[4] = {0, 1, 0, -1}, DY[4] = {-1, 0, 1, 0};
  g.step.assign(W * H * 4, -1);
  for (int y = 0; y < H; y++)
    for (int x = 0; x < W; x++)
      for (int d = 0; d < 4; d++) {
        int eid = edge_id(x, y, d);
        auto e = edges.find(eid);
        int typ = e == edges.end() ? 0 : e->second.first;
        int to;
        if (typ == 1) {
          to = -1;
        } else if (typ == 2 && partner.count(eid)) {
          int r = W + 1, pe = partner[eid];
          int i = pe / r, a = pe % r;
          int px = mod(a, W), py = i % 2 == 0 ? mod(i / 2, H) : mod((i - 1) / 2, H);
          if (d == 0) to = mod(py - 1, H) * W + px;
          else if (d == 3) to = py * W + mod(px - 1, W);
          else to = py * W + px;
        } else {
          to = mod(y + DY[d], H) * W + mod(x + DX[d], W);
        }
        g.step[(y * W + x) * 4 + d] = to;
      }
  return g;
}

// ---- Ctx ---------------------------------------------------------------------------------------------
std::vector<int> Ctx::ids(Team t) const {
  std::vector<int> out;
  for (size_t i = 0; i < st.alive.size(); i++)
    if (st.alive[i] && st.team[i] == "AB"[t]) out.push_back(int(i));
  return out;
}
std::vector<int> Ctx::lengths(Team t) const {
  std::vector<int> out;
  for (int i : ids(t)) out.push_back(int(st.body[i].size()));
  std::sort(out.rbegin(), out.rend());
  return out;
}
std::vector<int> Ctx::heads(Team t) const {
  std::vector<int> out;
  for (int i : ids(t)) out.push_back(st.body[i].front());
  return out;
}
int Ctx::queen(Team t) const {
  int q = queens_[t];
  return q >= 0 && q < (int)st.alive.size() && st.alive[q] ? q : -1;
}
const std::vector<char>& Ctx::occupied() const {
  if (!occ_done_) {
    occ_.assign(g.tiles(), 0);
    for (size_t i = 0; i < st.alive.size(); i++)
      if (st.alive[i])
        for (int tile : st.body[i]) occ_[tile] = 1;
    occ_done_ = true;
  }
  return occ_;
}
std::vector<int> Ctx::bfs(const std::vector<int>& sources, const std::vector<char>* blocked, int limit) const {
  std::vector<int> dist(g.tiles(), -1), frontier, nxt;
  for (int s : sources)
    if (dist[s] < 0) {
      dist[s] = 0;
      frontier.push_back(s);
    }
  int d = 0;
  while (!frontier.empty() && d < limit) {
    d++;
    nxt.clear();
    for (int t : frontier)
      for (int k = 0; k < 4; k++) {
        int n = g.go(t, k);
        if (n >= 0 && dist[n] < 0 && !(blocked && (*blocked)[n])) {
          dist[n] = d;
          nxt.push_back(n);
        }
      }
    frontier.swap(nxt);
  }
  return dist;
}
const std::vector<int>& Ctx::head_dist(Team t) const {
  if (!hd_done_[t]) {
    hd_[t] = bfs(heads(t));
    hd_done_[t] = true;
  }
  return hd_[t];
}
double Ctx::rate(Team t, const char* key, int window) const {
  const auto& hist = past_[t];
  int w = std::min<int>(window, (int)hist.size());
  if (w <= 0) return 0.0;
  auto get = [&](const Past& p) {
    if (!std::strcmp(key, "eat")) return p.eat;
    if (!std::strcmp(key, "deaths")) return p.deaths;
    if (!std::strcmp(key, "kills")) return p.kills;
    return p.splits;
  };
  const Counters& c = cum_[t];
  long long now = !std::strcmp(key, "eat") ? c.eat : !std::strcmp(key, "deaths") ? c.deaths
                : !std::strcmp(key, "kills") ? c.kills : c.splits;
  long long before = (int)hist.size() > w ? get(hist[hist.size() - w - 1]) : 0;
  return double(now - before) / w;
}

// ---- compute() -----------------------------------------------------------------------------------------
static const char* QUEEN_CAUSE_HEADON = "head-on";

static std::string queen_cause(const std::string& reason, const std::string& detail) {
  if (reason == "wall") return "hit a wall";
  if (reason == "self") return "ran into itself";
  if (reason == "other_body") return detail == "into_ally" ? "ran into an ally" : "ran into an enemy";
  if (reason == "head_to_head") {
    if (detail == "mover") return QUEEN_CAUSE_HEADON;
    if (detail.rfind("hit_by_enemy", 0) == 0)
      return detail.size() >= 6 && detail.compare(detail.size() - 6, 6, "_lunge") == 0
                 ? "head-on: lunged by an enemy" : "head-on: an enemy moved into it";
    return "head-on: an ally moved into it";
  }
  if (reason == "no_valid_action") return "no valid move";
  return "other";
}

static const char* DEATH[] = {"wall", "self", "other_body", "head_to_head", "no_valid_action"};

namespace {
struct Turn {                        // state.py walk's turn record
  int round = 0, id = 0;
  char team = 0;
  int act = -1;                      // -1 none, 0 move, 1 split, 2 suicide
  std::vector<int> dirs;
  int len_before = 0, head_before = 0, ate = 0;
  std::vector<int> path, killed;
  std::string died;                  // "" while alive
};
}  // namespace

Result compute(const std::string& file_bytes) {
  double t0 = now_s();
  thread_local std::string gunzipped;
  thread_local std::vector<uint64_t> words;
  const bool gz = file_bytes.size() >= 2 && (unsigned char)file_bytes[0] == 0x1f && (unsigned char)file_bytes[1] == 0x8b;
  if (gz) inflate_gzip(file_bytes, gunzipped);
  const std::string& blob = gz ? gunzipped : file_bytes;
  double t1 = now_s();
  unpack(blob.data(), blob.size(), words);
  Msg m{words, {}};
  double t2 = now_s();
  double snap_s = 0;
  if (m.words.empty()) throw std::runtime_error("empty replay");
  {
    uint32_t nseg = uint32_t(m.words[0] & 0xFFFFFFFF) + 1;
    std::vector<uint32_t> sizes;
    for (uint32_t s = 0; s < nseg; s++) {
      size_t half = 1 + s;
      uint64_t w = m.words.at(half / 2);
      sizes.push_back(uint32_t(half % 2 ? w >> 32 : w & 0xFFFFFFFF));
    }
    size_t first = (4 + 4 * nseg + 7) / 8;           // header words: 4 + 4n bytes, padded to 8
    size_t w = first;
    for (uint32_t s = 0; s < nseg; s++) {
      m.base.push_back(w);
      w += sizes[s];
    }
  }
  auto rp = m.resolve(0, 0);
  Struct root{&m, rp.seg, rp.target, unsigned((rp.w >> 32) & 0xFFFF), unsigned((rp.w >> 48) & 0xFFFF)};
  Result out;
  uint32_t fv = root.u32(0);
  (void)fv;
  Struct res = root.child(4);
  out.end = res ? (res.u16(2) == 0 ? "teamEliminated" : "roundLimit") : "";
  out.winner = res && res.u16(4) == 1 ? std::string(1, "AB"[res.u16(6) & 1]) : "";
  Map g = parse_map(root.text(0));
  out.map_name = g.name;
  out.W = g.W;
  out.H = g.H;

  const auto& reg = registry();
  std::vector<const Metric*> team_metrics, game_metrics;
  for (auto& mt : reg) (mt.derived ? game_metrics : team_metrics).push_back(&mt);
  for (auto& mt : reg) out.versions.push_back({mt.name, mt.version});

  // queens {team: id}: the first two dragons (a later one overrides, as a dict would)
  int queens[2] = {-1, -1};
  for (size_t i = 0; i < g.dragons.size() && i < 2; i++) queens[g.dragons[i].first == 'A' ? 0 : 1] = int(i);
  auto queen_team = [&](int id) { return id == queens[0] ? 0 : id == queens[1] ? 1 : -1; };

  State st;
  for (size_t i = 0; i < g.dragons.size(); i++) {
    st.ensure(int(i));
    st.body[i] = std::deque<int>(g.dragons[i].second.begin(), g.dragons[i].second.end());
    st.team[i] = g.dragons[i].first;
    st.alive[i] = 1;
  }
  st.pearl.assign(g.tiles(), 0);

  Counters cum[2];
  std::vector<Past> past[2];
  Explored explored[2];
  for (int t = 0; t < 2; t++) {
    explored[t].visited.assign(g.tiles(), 0);
    explored[t].seen.assign(g.tiles(), 0);
  }
  auto visit = [&](int t, int tile) {
    if (!explored[t].visited[tile]) {
      explored[t].visited[tile] = 1;
      explored[t].visited_count++;
    }
  };
  for (auto& [team, body] : g.dragons) visit(team == 'A' ? 0 : 1, body.at(0));

  out.team.resize(team_metrics.size());
  for (size_t k = 0; k < team_metrics.size(); k++) out.team[k].name = team_metrics[k]->name;
  out.game.resize(game_metrics.size());
  for (size_t k = 0; k < game_metrics.size(); k++) out.game[k].name = game_metrics[k]->name;

  auto snapshot = [&](int rnd) {
    double ts = now_s();
    struct Done { double& acc; double ts; ~Done() { acc += now_s() - ts; } } done{snap_s, ts};
    Ctx ctx(g, st, rnd, cum, past, explored, queens);
    Values values;
    for (size_t k = 0; k < team_metrics.size(); k++) {
      Value v[2];
      for (int t = 0; t < 2; t++) {
        v[t] = team_metrics[k]->team(ctx, Team(t));
        if (v[t].kind == Value::REAL) v[t].d = round4(v[t].d);
        out.team[k].series[t].push_back(v[t]);
      }
      values[team_metrics[k]->name] = {v[0], v[1]};
    }
    for (size_t k = 0; k < game_metrics.size(); k++) {
      Value x = game_metrics[k]->game(values, rnd);
      if (x.kind == Value::REAL) x.d = round4(x.d);
      out.game[k].series.push_back(x);
    }
    out.rounds.push_back(rnd);
  };

  // ---- the walk (state.py walk) and what compute() does with each step ----
  std::vector<Turn> rec_slot;
  bool have_rec = false;
  Turn rec;
  bool in_spawn = false;
  int last_done = 0;
  bool have_last = false;
  const int every = 10;

  auto on_turn = [&](Turn& r) {            // compute(): kind == "turn"
    int t = r.team == 'A' ? 0 : 1;
    cum[t].eat += r.ate;
    // it saw the board around its head, then moved
    int hx = r.head_before % g.W, hy = r.head_before / g.W, xs[7], rows[7];
    for (int k = 0; k < 7; k++) {
      xs[k] = mod(hx + k - 3, g.W);
      rows[k] = mod(hy + k - 3, g.H) * g.W;
    }
    auto& seen = explored[t].seen;
    for (int a = 0; a < 7; a++)
      for (int b = 0; b < 7; b++) {
        char& c = seen[rows[a] + xs[b]];
        if (!c) {
          c = 1;
          explored[t].seen_count++;
        }
      }
    visit(t, r.head_before);
    for (int p : r.path) visit(t, p);
    if (r.act == 0) {
      long long free = (r.len_before + 3) / 4;
      cum[t].paid += std::max<long long>(0, (long long)r.dirs.size() - free);
    }
    if (r.died == "head_to_head") {
      bool enemy = false;
      for (int k : r.killed) enemy |= (k < (int)st.team.size() ? st.team[k] : 0) != r.team;
      if (enemy) {
        cum[t].deaths_enemy++;
        cum[1 - t].kills++;
      } else {
        cum[t].deaths_self++;
      }
      int qt = queen_team(r.id);
      if (qt >= 0 && out.queen[qt].died && out.queen[qt].cause == QUEEN_CAUSE_HEADON)
        out.queen[qt].cause = enemy ? "head-on: moved into an enemy" : "head-on: moved into an ally";
    }
  };
  auto finish = [&]() {
    if (have_rec) {
      have_rec = false;
      on_turn(rec);
    }
  };
  auto round_end = [&](int r) {            // compute(): kind == "round_end"
    int rnd = r + 1;
    have_last = true;
    last_done = rnd;
    for (int t = 0; t < 2; t++) past[t].push_back({cum[t].eat, cum[t].deaths, cum[t].kills, cum[t].splits});
    if (rnd % every == 0 || rnd == 1) snapshot(rnd);
  };
  auto pt = [](const Struct& s) { return s ? std::pair<bool, std::pair<int, int>>{true, {s.i32(0), s.i32(4)}}
                                           : std::pair<bool, std::pair<int, int>>{false, {0, 0}}; };

  auto events = root.ptr(3);
  size_t n_events = 0;
  unsigned edw = 0, epc = 0;
  if (events.kind >= 0) {
    uint64_t tag = m.word(events.seg, events.target);
    n_events = (tag >> 2) & ((1u << 30) - 1);
    edw = unsigned((tag >> 32) & 0xFFFF);
    epc = unsigned((tag >> 48) & 0xFFFF);
  }
  for (size_t ei = 0; ei < n_events; ei++) {
    Struct e{&m, events.seg, events.target + 1 + ei * (edw + epc), edw, epc};
    unsigned k = e.u16(0);
    Struct p = e.child(0);
    if (k == 0 || k == 1) finish();
    if (k == 0) {                                          // roundStart
      if (st.round >= 0) round_end(st.round);
      st.round = p.i32(0);
      in_spawn = true;
    } else if (k == 1) {                                   // turnStart
      in_spawn = false;
      int cur = p.i32(0);
      st.ensure(cur);
      rec = Turn{};
      rec.round = st.round;
      rec.id = cur;
      rec.team = st.team[cur];
      rec.len_before = int(st.body[cur].size());
      rec.head_before = st.body[cur].empty() ? 0 : st.body[cur].front();
      have_rec = true;
    } else if (k == 4) {                                   // dragonAction
      if (have_rec) {
        Struct a = p.child(0);
        rec.act = -1;
        rec.dirs.clear();
        if (a) {
          unsigned w = a.u16(0);
          if (w == 0) {
            rec.act = 0;
            for (unsigned d : a.list_u16(0)) rec.dirs.push_back(int(d));
          } else {
            rec.act = w == 1 ? 1 : 2;
          }
        }
      }
    } else if (k == 3) {                                   // tileChange
      auto [has_pt, xy] = pt(p.child(0));
      if (!has_pt) continue;
      int tile = xy.second * g.W + xy.first;
      if (p.bit(0)) {
        st.pearl[tile] = 1;
        (void)in_spawn;
      } else {
        st.pearl[tile] = 0;
        if (have_rec) rec.ate++;
      }
    } else if (k == 9) {                                   // dragonUpdate
      int i = p.i32(0);
      if (st.round < 0 || !have_rec || i != rec.id) continue;
      auto [hh, head] = pt(p.child(0));
      auto [th, tail] = pt(p.child(1));
      int head_t = head.second * g.W + head.first, tail_t = tail.second * g.W + tail.first;
      auto& b = st.body[i];
      if (rec.act == 0) {
        int t = b.front();
        std::vector<int> path;
        for (int d : rec.dirs) {
          t = g.go(t, d);
          if (t < 0) break;
          path.push_back(t);
        }
        if (path.empty() || path.back() != head_t) path = {head_t};
        for (int q : path) b.push_front(q);
        rec.path = path;
        int guard = 0;
        while (!b.empty() && b.back() != tail_t && guard < 1000) {
          b.pop_back();
          guard++;
        }
      }
      (void)hh;
      (void)th;
    } else if (k == 10) {                                  // dragonSplit
      int par = p.i32(0), c = p.i32(4);
      char t = "AB"[p.u16(8) & 1];
      st.ensure(std::max(par, c));
      auto body_of = [&](unsigned idx) {
        std::deque<int> d;
        for (const Struct& s : p.list_struct(idx)) d.push_back(s.i32(4) * g.W + s.i32(0));
        return d;
      };
      st.body[par] = body_of(0);
      st.body[c] = body_of(1);
      st.team[c] = t;
      st.alive[c] = 1;
      cum[t == 'A' ? 0 : 1].splits++;                     // compute(): kind == "split"
    } else if (k == 11) {                                  // dragonDeath
      int i = p.i32(0);
      st.ensure(i);
      unsigned ri = p.u16(4);
      std::string reason = ri < 5 ? DEATH[ri] : "";
      std::string detail;
      if (reason == "head_to_head") {
        if (have_rec && i == rec.id) {
          detail = "mover";
        } else if (have_rec) {
          detail = std::string("hit_by_") + (st.team[rec.id] == st.team[i] ? "ally" : "enemy");
          if (rec.act == 0 && rec.dirs.size() > 1) detail += "_lunge";
          rec.killed.push_back(i);
        }
      } else if (reason == "other_body" && have_rec && i == rec.id && rec.act == 0) {
        // the mover died on the step that hit a body: find who lies there
        int t = st.body[i].empty() ? 0 : st.body[i].front();
        for (int d : rec.dirs) {
          int t2 = g.go(t, d);
          if (t2 < 0) break;
          t = t2;
          int occ = -1;
          for (size_t j = 0; j < st.alive.size() && occ < 0; j++)
            if (st.alive[j] && int(j) != i)
              for (int q : st.body[j])
                if (q == t) {
                  occ = int(j);
                  break;
                }
          if (occ >= 0) {
            detail = std::string("into_") + (st.team[occ] == st.team[i] ? "ally" : "enemy");
            break;
          }
        }
      } else if (reason == "no_valid_action") {
        int act = have_rec ? rec.act : -1;
        detail = act == 1 ? "split_fail" : (act == -1 || act == 2) ? "resign" : "bad_move";
      }
      // compute(): kind == "death" (before the dragon is removed)
      int t = st.team[i] == 'A' ? 0 : 1;
      int length = int(st.body[i].size());
      int qt = queen_team(i);
      if (qt >= 0) {
        out.queen[qt].died = true;
        out.queen[qt].round = st.round + 1;
        out.queen[qt].cause = queen_cause(reason, detail);
        out.queen[qt].length = length;
      }
      cum[t].deaths++;
      cum[t].lost += length;
      if (!(reason == "head_to_head" && detail == "mover")) {
        bool enemy = (reason == "head_to_head" && detail.rfind("hit_by_enemy", 0) == 0) ||
                     (reason == "other_body" && detail == "into_enemy");
        (enemy ? cum[t].deaths_enemy : cum[t].deaths_self)++;
        if (enemy) cum[1 - t].kills++;
      }
      if (have_rec && i == rec.id) rec.died = reason;
      st.alive[i] = 0;
    }
  }
  finish();
  round_end(st.round);
  if (have_last && (out.rounds.empty() || out.rounds.back() != last_done)) snapshot(last_done);
  (void)rec_slot;
  double t3 = now_s();
  phase_times.inflate += t1 - t0;
  phase_times.unpack += t2 - t1;
  phase_times.snapshots += snap_s;
  phase_times.walk += t3 - t2 - snap_s;
  return out;
}

// ---- JSON (the shape compute() returns) ------------------------------------------------------------
static void put_str(std::string& o, const std::string& s) {
  o += '"';
  for (size_t i = 0; i < s.size();) {
    unsigned char c = s[i];
    uint32_t cp;
    int n;
    if (c < 0x80) { cp = c; n = 1; }
    else if ((c >> 5) == 6 && i + 1 < s.size()) { cp = ((c & 0x1F) << 6) | (s[i + 1] & 0x3F); n = 2; }
    else if ((c >> 4) == 14 && i + 2 < s.size()) { cp = ((c & 0x0F) << 12) | ((s[i + 1] & 0x3F) << 6) | (s[i + 2] & 0x3F); n = 3; }
    else if ((c >> 3) == 30 && i + 3 < s.size()) {
      cp = ((c & 0x07) << 18) | ((s[i + 1] & 0x3F) << 12) | ((s[i + 2] & 0x3F) << 6) | (s[i + 3] & 0x3F); n = 4;
    } else { cp = 0xFFFD; n = 1; }
    i += n;
    char buf[16];
    if (cp == '"') o += "\\\"";
    else if (cp == '\\') o += "\\\\";
    else if (cp == '\n') o += "\\n";
    else if (cp == '\r') o += "\\r";
    else if (cp == '\t') o += "\\t";
    else if (cp < 0x20 || cp >= 0x7F) {
      if (cp >= 0x10000) {
        cp -= 0x10000;
        std::snprintf(buf, sizeof buf, "\\u%04x\\u%04x", 0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF));
      } else {
        std::snprintf(buf, sizeof buf, "\\u%04x", cp);
      }
      o += buf;
    } else {
      o += char(cp);
    }
  }
  o += '"';
}

static void put_value(std::string& o, const Value& v) {
  if (v.kind == Value::NONE) { o += "null"; return; }
  if (v.kind == Value::INT) { o += std::to_string(v.i); return; }
  if (!std::isfinite(v.d)) { o += std::isnan(v.d) ? "NaN" : v.d > 0 ? "Infinity" : "-Infinity"; return; }
  char buf[64];
  auto r = std::to_chars(buf, buf + sizeof buf, v.d);   // shortest round-trip, as Python's repr
  std::string s(buf, r.ptr);
  if (s.find_first_of(".eE") == std::string::npos) s += ".0";
  o += s;
}

std::string to_json(const Result& r) {
  std::string o = "{\"format\": 2, \"versions\": {";
  for (size_t i = 0; i < r.versions.size(); i++) {
    if (i) o += ", ";
    put_str(o, r.versions[i].first);
    o += ": " + std::to_string(r.versions[i].second);
  }
  o += "}, \"queens\": {";
  for (int t = 0; t < 2; t++) {
    if (t) o += ", ";
    o += t == 0 ? "\"A\": " : "\"B\": ";
    const auto& q = r.queen[t];
    if (!q.died) { o += "null"; continue; }
    o += "{\"round\": " + std::to_string(q.round) + ", \"cause\": ";
    put_str(o, q.cause);
    o += ", \"length\": " + std::to_string(q.length) + "}";
  }
  o += "}, \"every\": 10, \"rounds\": [";
  for (size_t i = 0; i < r.rounds.size(); i++) o += (i ? ", " : "") + std::to_string(r.rounds[i]);
  o += "], \"team\": {";
  for (size_t k = 0; k < r.team.size(); k++) {
    if (k) o += ", ";
    put_str(o, r.team[k].name);
    o += ": {";
    for (int t = 0; t < 2; t++) {
      o += t == 0 ? "\"A\": [" : ", \"B\": [";
      const auto& s = r.team[k].series[t];
      for (size_t i = 0; i < s.size(); i++) {
        if (i) o += ", ";
        put_value(o, s[i]);
      }
      o += "]";
    }
    o += "}";
  }
  o += "}, \"game\": {";
  for (size_t k = 0; k < r.game.size(); k++) {
    if (k) o += ", ";
    put_str(o, r.game[k].name);
    o += ": [";
    for (size_t i = 0; i < r.game[k].series.size(); i++) {
      if (i) o += ", ";
      put_value(o, r.game[k].series[i]);
    }
    o += "]";
  }
  o += "}, \"map\": {\"name\": ";
  put_str(o, r.map_name);
  o += ", \"W\": " + std::to_string(r.W) + ", \"H\": " + std::to_string(r.H) + "}, \"result\": {\"winner\": ";
  if (r.winner.empty()) o += "null"; else put_str(o, r.winner);
  o += ", \"end\": ";
  if (r.end.empty()) o += "null"; else put_str(o, r.end);
  o += "}, \"errors\": {}}";
  return o;
}

std::string describe_json() {
  std::string o = "[";
  bool first = true;
  for (auto& m : registry()) {
    if (!first) o += ", ";
    first = false;
    o += "{\"name\": ";
    put_str(o, m.name);
    o += ", \"title\": ";
    put_str(o, m.title);
    o += ", \"desc\": ";
    put_str(o, m.desc);
    o += ", \"version\": " + std::to_string(m.version) + ", \"unit\": ";
    put_str(o, m.unit);
    o += ", \"better\": ";
    put_str(o, m.better);
    o += ", \"kind\": ";
    o += m.derived ? "\"game\"" : "\"team\"";
    o += ", \"fitted\": ";
    if (m.fitted.empty()) o += "null"; else put_str(o, m.fitted);
    o += "}";
  }
  return o + "]";
}

}  // namespace am
