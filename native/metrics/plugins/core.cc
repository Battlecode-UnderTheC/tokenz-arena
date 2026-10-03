// Counting metrics: dragons, lengths, the queen, pearls, deaths and kills (arena/metrics/plugins/core.py).
#include <cmath>

#include "../metric.h"

using namespace am;

METRIC(dragons, 100, "Dragons alive", "dragons the team has alive", 1, "dragons", "higher") {
  return Value::of((long long)ctx.ids(t).size());
}

METRIC(log_dragons, 101, "Dragons alive (log)", "log(1 + dragons alive): what predicts winning "
       "through mid-game (each extra dragon is worth less than the one before)", 1, "", "higher") {
  return Value::real(std::log1p(double(ctx.ids(t).size())));
}

METRIC(total_length, 102, "Total length", "sum of the team's dragons' lengths", 1, "cells", "higher") {
  long long s = 0;
  for (int l : ctx.lengths(t)) s += l;
  return Value::of(s);
}

METRIC(longest, 103, "Longest dragon", "the team's longest dragon: decides round 500 in most games", 1, "cells", "higher") {
  auto ls = ctx.lengths(t);
  return Value::of(ls.empty() ? 0 : ls[0]);
}

METRIC(queen_alive, 104, "Queen alive", "1 if the team's queen (dragon 0 or 1) is alive", 1, "", "higher") {
  return Value::of(ctx.queen(t) >= 0 ? 1 : 0);
}

METRIC(queen_length, 105, "Queen length", "the queen's length (0 once dead): the first round-500 "
       "tiebreak", 1, "cells", "higher") {
  int q = ctx.queen(t);
  return Value::of(q >= 0 ? (long long)ctx.st.body[q].size() : 0);
}

METRIC(pearls_eaten, 106, "Pearls eaten", "pearls eaten so far", 1, "pearls", "higher") {
  return Value::of(ctx.cum(t).eat);
}

METRIC(pearl_rate, 107, "Pearl rate", "pearls eaten per round over the last 50 rounds: how fast the "
       "team farms now (pearls eaten counts the whole game)", 1, "pearls/round", "higher") {
  return Value::real(round4(ctx.rate(t, "eat", 50)));
}

METRIC(deaths, 108, "Deaths", "own dragons lost so far (more deaths go with winning: active bots split and die more)",
       1, "dragons", "none") {
  return Value::of(ctx.cum(t).deaths);
}

METRIC(deaths_self, 109, "Self-inflicted deaths", "own dragons lost to walls, itself, allies or "
       "having no valid move", 1, "dragons", "none") {
  return Value::of(ctx.cum(t).deaths_self);
}

METRIC(kills, 110, "Kills", "enemy dragons this team killed (on its own, kills don't predict "
       "winning: killers trade)", 1, "dragons", "higher") {
  return Value::of(ctx.cum(t).kills);
}

METRIC(splits, 111, "Splits", "splits so far", 1, "splits", "none") {
  return Value::of(ctx.cum(t).splits);
}
