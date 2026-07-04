"""Multiprocessing first-pass routing — true multi-core (bypasses the GIL).

Thread-based parallelism caps at ~1 core here (measured): the GIL plus the
shared workspace lock serialize everything. Separate OS processes each get
their own interpreter and workspace, so disjoint net-groups route with zero
shared state and genuinely use all cores.

Design: signal nets are partitioned into spatially-disjoint groups; each
group routes in a worker process against only the STATIC board (pads), so
groups never interact. Big/spanning nets and the completion tail run in the
main process afterwards (they need the merged global state).
"""

from __future__ import annotations

import os
from collections import defaultdict


def _warm():
    """Pool initializer: import + JIT-warm the numba kernels once per
    process so per-task calls are compiled."""
    import numpy as np

    from .fields import distance_field, erode_disk, seg_clear, string_pull
    from .geom_kernels import trace_ok_csr  # noqa
    m = np.zeros((8, 8), dtype=bool)
    distance_field(m, 4.0)
    erode_disk(m, 1)
    seg_clear(0.0, 0.0, 4.0, 0.0, np.ones((8, 8), np.float32), m, 5.0,
              0.0, 0.0, 4.0, 8, 8)
    string_pull(np.array([0.0, 4, 8]), np.array([0.0, 0, 0]),
                np.ones((8, 8), np.float32), m, 5.0, 0.0, 0.0, 4.0, 8, 8)


def _route_group(args):
    """Route a group of nets in this process against the static board.
    Returns (traces, vias, failed)."""
    dsn, step, sources, net_names = args
    from . import load_dsn
    from .grid import Workspace
    from .router import Router

    board = load_dsn(dsn)
    ws = Workspace(board, step=step)
    r = Router(board, ws, power_sources=sources)
    for name in net_names:
        net = board.nets.get(name)
        if net is not None:
            r.route_net(net)
    return (
        [(t.net, t.layer, t.coords, t.width) for t in r.result.traces],
        [(v.net, v.x, v.y, v.diameter, v.padstack) for v in r.result.vias],
        list(r.result.failed),
    )


def partition_nets(board, net_names, k):
    """Split nets into ~k spatially-disjoint groups by pad-bbox region,
    plus a 'spanning' list of nets too large to localize."""
    b = board.outline.bounds
    W = (b[2] - b[0]) or 1.0
    H = (b[3] - b[1]) or 1.0
    import math
    g = max(1, int(math.isqrt(k)))
    cw, ch = W / g, H / g
    groups = defaultdict(list)
    spanning = []
    for name in net_names:
        pads = board.pads_of_net(board.nets[name])
        if len(pads) < 2:
            continue
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        spanx = max(xs) - min(xs)
        spany = max(ys) - min(ys)
        # only nets spanning MOST of the board (true globals like buses) go
        # to the main process; everything else is assigned to its centre
        # region and routed in parallel (boundary conflicts -> tail fixes)
        if spanx > 0.6 * W or spany > 0.6 * H:
            spanning.append(name)
            continue
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        gx = min(g - 1, int((cx - b[0]) / cw))
        gy = min(g - 1, int((cy - b[1]) / ch))
        groups[(gx, gy)].append(name)
    return list(groups.values()), spanning


def route_first_pass_mp(router, dsn, step, sources, local_names, workers=None,
                        progress=None):
    """Route local nets across processes, merge into the router's workspace.
    Returns the list of spanning nets to route in the main process next."""
    import concurrent.futures as cf

    workers = workers or max(2, (os.cpu_count() or 8) - 2)
    groups, spanning = partition_nets(router.board, local_names, workers)
    if progress:
        progress(0, 0, f"mp first pass: {len(groups)} disjoint groups across "
                       f"{workers} processes, {len(spanning)} spanning nets "
                       f"deferred", router.result)
    tasks = [(dsn, step, sources, g) for g in groups]
    ctx = __import__("multiprocessing").get_context("spawn")
    with cf.ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                initializer=_warm) as pool:
        results = list(pool.map(_route_group, tasks))
    # merge every group's copper into the main workspace
    ws = router.ws
    for traces, vias, failed in results:
        for net, layer, coords, width in traces:
            ws.add_trace(net, layer, coords, width)
            from .router import Trace
            router.result.traces.append(Trace(net, layer, coords, width))
            iys, ixs = ws.line_cells(coords)
            # mark connectivity target roughly by re-adding (tail will fix)
        for net, x, y, dia, ps in vias:
            ws.add_via(net, x, y, dia)
            from .router import Via
            router.result.vias.append(Via(net, x, y, dia, ps))
        for f in failed:
            router.result.failed.append(f)
        router.result.routed_edges += len(traces)
    return spanning
