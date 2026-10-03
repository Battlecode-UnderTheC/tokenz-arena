// arena-metrics: the in-game metrics (arena/metrics) in C++, for thousands of replays a second.
//
// A metric is a function in a file under plugins/, registered like the Python @metric:
//
//     METRIC(tiles_visited, 300, "Tiles visited", "share of the board's tiles ...", 1, "share", "higher") {
//         return share(ctx.explored(t).visited_count, ctx.tiles());
//     }
//
// It's called for each team every 10 rounds (and at the game's last round) with a Ctx: the replayed
// state at that round plus helpers, the same as Python's (arena/metrics/__init__.py Ctx). Derived
// metrics (DERIVED) combine the team metrics' values at a round into one number for the game.
//
// The Python plugins stay the reference and the place to try a metric first: the parity check
// (native/metrics/parity.py) compares every metric here with its Python twin on stored replays,
// and arena/metrics/native.py uses this binary only when it has every registered Python metric at
// the same version.
#pragma once

#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <string>
#include <vector>

namespace am {

enum Team { A = 0, B = 1 };
inline Team other(Team t) { return t == A ? B : A; }

// A metric's value: an integer, a float (rounded to 4 places as Python's round(x, 4) does), or none.
struct Value {
  enum Kind { NONE, INT, REAL } kind = NONE;
  long long i = 0;
  double d = 0;
  static Value of(long long v) { Value x; x.kind = INT; x.i = v; return x; }
  static Value real(double v) { Value x; x.kind = REAL; x.d = v; return x; }
  double num() const { return kind == INT ? double(i) : kind == REAL ? d : 0.0; }
};

double round4(double x);   // Python's round(x, 4)

// ---- the board (state.py GameMap) ----------------------------------------------------------------
struct Map {
  std::string name = "?";
  int W = 0, H = 0;
  std::vector<int> step;                              // tile*4 + dir (N E S W) -> tile, -1 for kelp
  std::vector<std::pair<char, std::vector<int>>> dragons;   // (team 'A'/'B', body head first)
  int tiles() const { return W * H; }
  int go(int tile, int d) const { return step[tile * 4 + d]; }
};

// ---- the replayed state (state.py State) -------------------------------------------------------
struct State {
  std::vector<std::deque<int>> body;   // by dragon id
  std::vector<char> team;              // 'A' / 'B' / 0 for an id never seen
  std::vector<char> alive;
  std::vector<char> pearl;             // by tile
  int round = -1;
  void ensure(int id) {
    if (id >= (int)body.size()) {
      body.resize(id + 1);
      team.resize(id + 1, 0);
      alive.resize(id + 1, 0);
    }
  }
};

struct Counters {                      // compute()'s cum[team]
  long long eat = 0, deaths = 0, deaths_enemy = 0, deaths_self = 0, lost = 0, splits = 0, kills = 0, paid = 0;
};
struct Past {                          // compute()'s past[team][i]
  long long eat = 0, deaths = 0, kills = 0, splits = 0;
};
struct Explored {
  std::vector<char> visited, seen;
  int visited_count = 0, seen_count = 0;
};

// ---- what a metric sees (metrics/__init__.py Ctx) ------------------------------------------------
class Ctx {
 public:
  Ctx(const Map& g, const State& st, int rnd, const Counters* cum, const std::vector<Past>* past,
      const Explored* explored, const int* queens)
      : g(g), st(st), rnd(rnd), cum_(cum), past_(past), explored_(explored), queens_(queens) {}

  const Map& g;
  const State& st;
  int rnd;

  int tiles() const { return g.tiles(); }
  const Counters& cum(Team t) const { return cum_[t]; }
  const Explored& explored(Team t) const { return explored_[t]; }
  std::vector<int> ids(Team t) const;
  std::vector<int> lengths(Team t) const;                 // longest first
  std::vector<int> heads(Team t) const;
  int queen(Team t) const;                                // the queen's id if alive, else -1
  const std::vector<char>& occupied() const;              // by tile
  // Steps from any of `sources` to each tile (-1: unreached); `blocked` by tile (or empty).
  std::vector<int> bfs(const std::vector<int>& sources, const std::vector<char>* blocked = nullptr,
                       int limit = 1 << 30) const;
  const std::vector<int>& head_dist(Team t) const;        // bfs from the team's heads (cached)
  // How much counter `key` (eat, deaths, kills, splits) grew per round over the last `window` rounds.
  double rate(Team t, const char* key, int window) const;

 private:
  const Counters* cum_;
  const std::vector<Past>* past_;
  const Explored* explored_;
  const int* queens_;
  mutable std::vector<char> occ_;
  mutable bool occ_done_ = false;
  mutable std::vector<int> hd_[2];
  mutable bool hd_done_[2] = {false, false};
};

// ---- the registry -----------------------------------------------------------------------------------
using TeamFn = std::function<Value(const Ctx&, Team)>;
using Values = std::map<std::string, std::pair<Value, Value>>;   // metric -> (A, B) at this round
using GameFn = std::function<Value(const Values&, int)>;

struct Metric {
  std::string name, title, desc, unit, better;
  int version = 1;
  bool derived = false;
  std::string fitted;                 // a fitted derived metric: the newest game its fit saw
  TeamFn team;
  GameFn game;
};

std::vector<Metric>& registry();      // in the order the Python plugins register (module name order)

struct Register {
  Register(int order, Metric m);
};

}  // namespace am

// ORDER puts metrics in the Python registry's order (plugins imported by module name, then in file
// order), so the output lists them the same way: the module's rank * 100 + its place in the file
// (control 0xx, core 1xx, eval 2xx, explore 3xx, queen 4xx).
#define AM_CAT2(a, b) a##b
#define AM_CAT(a, b) AM_CAT2(a, b)
#define METRIC(NAME, ORDER, TITLE, DESC, VERSION, UNIT, BETTER)                                     \
  static am::Value AM_CAT(metric_, NAME)(const am::Ctx& ctx, am::Team t);                           \
  static am::Register AM_CAT(reg_, NAME)(ORDER, am::Metric{#NAME, TITLE, DESC, UNIT, BETTER, VERSION, \
                                                           false, "", AM_CAT(metric_, NAME), nullptr}); \
  static am::Value AM_CAT(metric_, NAME)([[maybe_unused]] const am::Ctx& ctx, [[maybe_unused]] am::Team t)
#define DERIVED(NAME, ORDER, TITLE, DESC, VERSION, UNIT, BETTER, FITTED)                              \
  static am::Value AM_CAT(metric_, NAME)(const am::Values& values, int rnd);                         \
  static am::Register AM_CAT(reg_, NAME)(ORDER, am::Metric{#NAME, TITLE, DESC, UNIT, BETTER, VERSION, \
                                                           true, FITTED, nullptr, AM_CAT(metric_, NAME)}); \
  static am::Value AM_CAT(metric_, NAME)([[maybe_unused]] const am::Values& values, [[maybe_unused]] int rnd)
