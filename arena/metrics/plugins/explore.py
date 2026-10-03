"""Exploration: how much of the board a team has been to and has seen, whatever the opponent
does (board_control, by contrast, is relative: tiles reached before the enemy)."""

from arena.metrics import metric


@metric("tiles_visited", "Tiles visited", "share of the board's tiles the team's dragon heads "
        "have stood on so far (sprints count every tile they pass)", unit="share")
def tiles_visited(ctx, t):
    return round(len(ctx.explored[t]["visited"]) / (ctx.g.W * ctx.g.H), 4)


@metric("tiles_seen", "Tiles seen", "share of the board's tiles that have been in one of the "
        "team's dragons' 7x7 views so far", unit="share")
def tiles_seen(ctx, t):
    return round(len(ctx.explored[t]["seen"]) / (ctx.g.W * ctx.g.H), 4)
