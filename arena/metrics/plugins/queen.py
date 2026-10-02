"""Queen metrics: under 1.2.3 the queens (dragons 0 and 1) decide round 500 first, and among the
top teams a lone surviving queen wins 75% of games at r200 and 99% at r490."""

from arena.metrics import metric

CAP = 30


@metric("queen_room", "Queen room", "free tiles the queen's head can reach within 8 steps "
        "(0 once dead): a boxed-in queen is about to die", unit="tiles")
def queen_room(ctx, t):
    q = ctx.queen(t)
    if q is None:
        return 0
    head = ctx.st.body[q][0]
    return len(ctx.bfs([head], blocked=ctx.occupied() - {head}, limit=8)) - 1


@metric("queen_danger", "Queen distance to enemy", "steps from the nearest enemy head to the "
        f"queen's head, capped at {CAP} (0 once dead)", unit="steps")
def queen_danger(ctx, t):
    q = ctx.queen(t)
    if q is None:
        return 0
    return min(CAP, ctx.head_dist(ctx.other(t)).get(ctx.st.body[q][0], CAP))


@metric("queen_share", "Queen's share of length", "the queen's length over the team's total "
        "(0 once dead): how much the team invests in its queen", unit="share")
def queen_share(ctx, t):
    q = ctx.queen(t)
    total = sum(ctx.lengths(t))
    return round(len(ctx.st.body[q]) / total, 4) if q is not None and total else 0
