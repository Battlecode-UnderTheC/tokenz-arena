"""Board metrics: who reaches the board and the pearls first."""

from arena.metrics import metric


@metric("board_control", "Board control", "share of all tiles this team's heads reach before "
        "the enemy's: the best single predictor of the winner from round ~50 to ~350",
        unit="share")
def board_control(ctx, t):
    mine, theirs = ctx.head_dist(t), ctx.head_dist(ctx.other(t))
    big = 10 ** 9
    won = sum(1 for tile, d in mine.items() if d < theirs.get(tile, big))
    return round(won / (ctx.g.W * ctx.g.H), 4)


@metric("pearl_control", "Pearls nearer", "pearls on the board this team's heads reach first",
        unit="pearls")
def pearl_control(ctx, t):
    mine, theirs = ctx.head_dist(t), ctx.head_dist(ctx.other(t))
    big = 10 ** 9
    return sum(1 for p in ctx.st.pearls if mine.get(p, big) < theirs.get(p, big))
