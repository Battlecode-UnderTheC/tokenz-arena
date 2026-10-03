// The eval bar: P(team A wins) at each round (arena/metrics/plugins/eval.py, v2).
#include <cmath>
#include <array>
#include <iterator>
#include <map>

#include "../metric.h"

using namespace am;

// round -> weights for Δlog(1+dragons), Δboard control, Δlongest, Δqueen alive, leader now
static const std::map<int, std::array<double, 5>> WEIGHTS = {
    {10, {0.417, 1.531, 0.035, 0.213, 0.210}}, {20, {1.066, 1.947, 0.080, 0.117, 0.172}},
    {50, {1.330, 2.178, 0.112, 0.195, 0.155}}, {100, {1.045, 1.892, 0.098, 0.301, 0.109}},
    {150, {0.907, 1.436, 0.047, 0.370, 0.299}}, {200, {0.766, 1.387, 0.029, 0.750, 0.255}},
    {250, {0.622, 1.309, 0.020, 0.818, 0.384}}, {300, {0.490, 1.402, 0.021, 1.194, 0.339}},
    {350, {0.250, 1.943, 0.022, 1.569, 0.336}}, {400, {0.079, 2.030, 0.030, 1.880, 0.583}},
    {450, {-0.150, 1.989, 0.037, 2.243, 1.102}}, {490, {-0.164, 1.574, 0.045, 3.455, 2.166}},
};

static std::array<double, 5> weights(int rnd) {
  int first = WEIGHTS.begin()->first, last = WEIGHTS.rbegin()->first;
  if (rnd <= first) return WEIGHTS.at(first);
  if (rnd >= last) return WEIGHTS.at(last);
  int hi = WEIGHTS.lower_bound(rnd)->first;
  int lo = std::prev(WEIGHTS.upper_bound(rnd))->first;
  if (hi == lo) return WEIGHTS.at(lo);
  double f = double(rnd - lo) / (hi - lo);
  std::array<double, 5> out;
  const auto &a = WEIGHTS.at(lo), &b = WEIGHTS.at(hi);
  for (int i = 0; i < 5; i++) out[i] = a[i] + f * (b[i] - a[i]);
  return out;
}

static double get(const Values& v, const char* k, int team) {
  auto it = v.find(k);
  if (it == v.end()) return 0;
  const Value& x = team == 0 ? it->second.first : it->second.second;
  return x.num();
}

// +1 if A leads under the round-500 rule now (queen, then longest, then total), -1 if B
static int leader(const Values& v) {
  for (const char* k : {"queen_length", "longest", "total_length"}) {
    double a = get(v, k, 0), b = get(v, k, 1);
    if (a != b) return a > b ? 1 : -1;
  }
  return 0;
}

DERIVED(eval, 200, "Eval: P(A wins)", "chance team A wins, from dragons alive, board control, "
        "longest dragon, queen alive and who leads under the round-500 rule", 2, "probability", "none",
        "2026-10-02T04:40:00Z") {
  if (rnd >= 500) {                      // the game is over: the result, not a prediction
    int lead = leader(values);
    return Value::real(lead > 0 ? 1.0 : lead < 0 ? 0.0 : 0.5);
  }
  auto w = weights(rnd);
  auto d = [&](const char* k) { return get(values, k, 0) - get(values, k, 1); };
  double z = w[0] * d("log_dragons") + w[1] * d("board_control") + w[2] * d("longest")
           + w[3] * d("queen_alive") + w[4] * leader(values);
  if (get(values, "dragons", 0) == 0 || get(values, "dragons", 1) == 0)   // someone's eliminated
    return Value::real(get(values, "dragons", 1) == 0 && get(values, "dragons", 0) ? 1.0 : 0.0);
  return Value::real(1 / (1 + std::exp(-z)));
}
