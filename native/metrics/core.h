// arena-metrics' core: one replay in, compute()'s result out (arena/metrics/__init__.py compute).
#pragma once

#include <string>
#include <utility>
#include <vector>

#include "metric.h"

namespace am {

struct Series {
  std::string name;
  std::vector<Value> series[2];      // team metrics: A, B
};
struct GameSeries {
  std::string name;
  std::vector<Value> series;
};
struct QueenDeath {
  bool died = false;
  int round = 0, length = 0;
  std::string cause;
};
struct Result {
  std::vector<std::pair<std::string, int>> versions;
  QueenDeath queen[2];
  std::vector<int> rounds;
  std::vector<Series> team;
  std::vector<GameSeries> game;
  std::string map_name, winner, end;
  int W = 0, H = 0;
};

struct PhaseTimes { double inflate = 0, unpack = 0, walk = 0, snapshots = 0; };
extern PhaseTimes phase_times;   // summed over compute() calls, for --bench (one thread)

Map parse_map(const std::string& text);
Result compute(const std::string& file_bytes);   // a replay file's bytes, plain or gzip
std::string to_json(const Result& r);
std::string describe_json();
std::string replay_info(const std::string& file_bytes);   // "seed<TAB>botA<TAB>botB"                     // the registry, as metrics.describe() lists it

}  // namespace am
