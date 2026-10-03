// Board metrics: who reaches the board and the pearls first (arena/metrics/plugins/control.py).
#include "../metric.h"

using namespace am;

METRIC(board_control, 0, "Board control", "share of all tiles this team's heads reach before "
       "the enemy's: the best single predictor of the winner from round ~50 to ~350", 1, "share", "higher") {
  const auto& mine = ctx.head_dist(t);
  const auto& theirs = ctx.head_dist(other(t));
  long long won = 0;
  for (int tile = 0; tile < ctx.tiles(); tile++)
    if (mine[tile] >= 0 && (theirs[tile] < 0 || mine[tile] < theirs[tile])) won++;
  return Value::real(double(won) / ctx.tiles());
}

METRIC(pearl_control, 1, "Pearls nearer", "pearls on the board this team's heads reach first", 1, "pearls", "higher") {
  const auto& mine = ctx.head_dist(t);
  const auto& theirs = ctx.head_dist(other(t));
  long long n = 0;
  for (int tile = 0; tile < ctx.tiles(); tile++)
    if (ctx.st.pearl[tile] && mine[tile] >= 0 && (theirs[tile] < 0 || mine[tile] < theirs[tile])) n++;
  return Value::of(n);
}
