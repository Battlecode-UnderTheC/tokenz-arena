// Queen metrics (arena/metrics/plugins/queen.py).
#include <algorithm>

#include "../metric.h"

using namespace am;

static const int CAP = 30;

METRIC(queen_room, 400, "Queen room", "free tiles the queen's head can reach within 8 steps "
       "(0 once dead): a boxed-in queen is about to die", 1, "tiles", "higher") {
  int q = ctx.queen(t);
  if (q < 0) return Value::of(0);
  int head = ctx.st.body[q].front();
  std::vector<char> blocked = ctx.occupied();
  blocked[head] = 0;
  auto dist = ctx.bfs({head}, &blocked, 8);
  long long n = 0;
  for (int d : dist) n += d >= 0;
  return Value::of(n - 1);
}

METRIC(queen_danger, 401, "Queen distance to enemy", "steps from the nearest enemy head to the "
       "queen's head, capped at 30 (0 once dead)", 1, "steps", "higher") {
  int q = ctx.queen(t);
  if (q < 0) return Value::of(0);
  int d = ctx.head_dist(other(t))[ctx.st.body[q].front()];
  return Value::of(std::min(CAP, d < 0 ? CAP : d));
}

METRIC(queen_share, 402, "Queen's share of length", "the queen's length over the team's total "
       "(0 once dead): how much the team invests in its queen", 1, "share", "higher") {
  int q = ctx.queen(t);
  long long total = 0;
  for (int l : ctx.lengths(t)) total += l;
  if (q < 0 || !total) return Value::of(0);
  return Value::real(round4(double(ctx.st.body[q].size()) / total));
}
