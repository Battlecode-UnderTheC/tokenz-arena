// Exploration: how much of the board a team has been to and has seen (arena/metrics/plugins/explore.py).
#include "../metric.h"

using namespace am;

METRIC(tiles_visited, 300, "Tiles visited", "share of the board's tiles the team's dragon heads "
       "have stood on so far (sprints count every tile they pass)", 1, "share", "higher") {
  return Value::real(double(ctx.explored(t).visited_count) / ctx.tiles());
}

METRIC(tiles_seen, 301, "Tiles seen", "share of the board's tiles that have been in one of the "
       "team's dragons' 7x7 views so far", 1, "share", "higher") {
  return Value::real(double(ctx.explored(t).seen_count) / ctx.tiles());
}
