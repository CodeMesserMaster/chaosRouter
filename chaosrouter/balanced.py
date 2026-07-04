"""Congestion-balanced layer routing (chaosRouter).

An additional method that spreads nets across the available signal layers
WITHOUT directional (Manhattan) lanes — every net still takes its shortest
curved path, it just takes it on whichever signal layer is least congested in
that net's own region. Layers fill evenly because each net prefers the
emptiest one, not because it's forced into a direction. Traces stay short;
layer changes happen only where congestion genuinely requires them.

The proven completion tail (rip-up / shake / endgame) is reused unchanged.
"""

from __future__ import annotations

import numpy as np

FREE = -1


def _least_congested_layer(router, net):
    """The signal layer with the fewest routed TRACE/VIA cells in this net's
    pad bounding box (pads excluded via wiring_owner, so pad fields don't
    skew the choice)."""
    ws = router.ws
    pads = router.board.pads_of_net(net)
    if not pads:
        return ws.layers[0]
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    ix0, iy0 = ws.to_cell(min(xs), min(ys))
    ix1, iy1 = ws.to_cell(max(xs), max(ys))
    ix0, ix1 = sorted((ix0, ix1))
    iy0, iy1 = sorted((iy0, iy1))
    best, best_occ = ws.layers[0], None
    for ly in ws.layers:
        region = ws.wiring_owner[ly][iy0 : iy1 + 1, ix0 : ix1 + 1]
        occ = int(np.count_nonzero(region != FREE))
        if best_occ is None or occ < best_occ:
            best, best_occ = ly, occ
    return best


def route_all_balanced(router, progress=None):
    """Route every net on its least-congested signal layer (shortest path),
    then run the standard completion tail."""
    from .diffpair import find_diff_pairs, route_diff_pair

    board = router.board

    # differential pairs first (unchanged)
    for net_p, net_n, gap in find_diff_pairs(board):
        if route_diff_pair(router, net_p, net_n, gap):
            router.result.diffpair_nets |= {net_p.name, net_n.name}

    nets = [n for n in router.net_order() if n.name not in router.result.diffpair_nets]
    # shorter nets first: they lock in the emptiest layers cheaply and leave
    # the long/hard nets maximum freedom
    def span(n):
        pads = board.pads_of_net(n)
        if len(pads) < 2:
            return 0.0
        xs = [p.x for p in pads]; ys = [p.y for p in pads]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))

    nets.sort(key=span)

    n = len(nets)
    for i, net in enumerate(nets):
        net.pref_layers = [_least_congested_layer(router, net)]
        try:
            router.route_net(net)
        finally:
            net.pref_layers = None
        if progress and (i % 10 == 0 or i == n - 1):
            progress(i + 1, n, net.name, router.result)

    # proven completion tail
    router._rip_and_retry(progress)
    router._shake_parallel(progress)
    router._endgame(progress)
    if router.result.failed:
        router._shake_parallel(progress)
    return router.result
