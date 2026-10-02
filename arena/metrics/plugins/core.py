"""Counting metrics: dragons, lengths, the queen, pearls, deaths and kills."""

import math

from arena.metrics import metric


@metric("dragons", "Dragons alive", "dragons the team has alive", unit="dragons")
def dragons(ctx, t):
    return len(ctx.ids(t))


@metric("log_dragons", "Dragons alive (log)", "log(1 + dragons alive): what predicts winning "
        "through mid-game (each extra dragon is worth less than the one before)")
def log_dragons(ctx, t):
    return round(math.log1p(len(ctx.ids(t))), 4)


@metric("total_length", "Total length", "sum of the team's dragons' lengths", unit="cells")
def total_length(ctx, t):
    return sum(ctx.lengths(t))


@metric("longest", "Longest dragon", "the team's longest dragon: decides round 500 in most games",
        unit="cells")
def longest(ctx, t):
    ls = ctx.lengths(t)
    return ls[0] if ls else 0


@metric("queen_alive", "Queen alive", "1 if the team's queen (dragon 0 or 1) is alive")
def queen_alive(ctx, t):
    return int(ctx.queen(t) is not None)


@metric("queen_length", "Queen length", "the queen's length (0 once dead): the first round-500 "
        "tiebreak", unit="cells")
def queen_length(ctx, t):
    q = ctx.queen(t)
    return len(ctx.st.body[q]) if q is not None else 0


@metric("pearls_eaten", "Pearls eaten", "pearls eaten so far", unit="pearls")
def pearls_eaten(ctx, t):
    return ctx.cum[t]["eat"]


@metric("deaths", "Deaths", "own dragons lost so far (more deaths go with winning: active bots split and die more)",
         unit="dragons", better="none")
def deaths(ctx, t):
    return ctx.cum[t]["deaths"]


@metric("deaths_self", "Self-inflicted deaths", "own dragons lost to walls, itself, allies or "
        "having no valid move", unit="dragons", better="none")
def deaths_self(ctx, t):
    return ctx.cum[t]["deaths_self"]


@metric("kills", "Kills", "enemy dragons this team killed (on its own, kills don't predict "
        "winning: killers trade)", unit="dragons")
def kills(ctx, t):
    return ctx.cum[t]["kills"]


@metric("splits", "Splits", "splits so far", unit="splits", better="none")
def splits(ctx, t):
    return ctx.cum[t]["splits"]
