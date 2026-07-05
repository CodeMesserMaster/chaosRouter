"""Strategic routing planner (chaosRouter).

Implements the veteran manual method: cluster nearby components, route the
LOCAL island connections first (flat, on component layers), then power/
ground, then the LONG inter-cluster routes with layer freedom. Diff pairs go
first (handled by diffpair.py). See memory: routing-methodology.

This module currently provides the PLANNING primitives (clustering + net
classification). The phased router is built on top.
"""

from __future__ import annotations

import math
from collections import defaultdict


def component_clusters(board, gap=300.0, local_pins=8):
    """A cluster = components that are BOTH close together AND directly
    interconnected (the user's definition). Two components merge only if they
    share a LOCAL signal net (<= `local_pins` pads, so power/ground don't drag
    the whole board in) AND their bounding boxes are within `gap` mil. Returns
    {comp_ref: cluster_id}."""
    by_ref = defaultdict(list)
    for p in board.pads.values():
        by_ref[p.ref].append(p)
    refs = list(by_ref)
    bbox = {}
    for ref, pads in by_ref.items():
        xs = [p.x for p in pads]; ys = [p.y for p in pads]
        bbox[ref] = (min(xs), min(ys), max(xs), max(ys))

    parent = {r: r for r in refs}
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def near(a, b):
        ax0, ay0, ax1, ay1 = bbox[a]
        bx0, by0, bx1, by1 = bbox[b]
        dx = max(0.0, max(bx0 - ax1, ax0 - bx1))
        dy = max(0.0, max(by0 - ay1, ay0 - by1))
        return dx <= gap and dy <= gap

    # merge components sharing a local net, but only when physically close
    for net in board.nets.values():
        pads = board.pads_of_net(net)
        if not (2 <= len(pads) <= local_pins):
            continue
        rs = list({p.ref for p in pads})
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                if near(rs[i], rs[j]):
                    union(rs[i], rs[j])

    cluster_of = {}
    ids = {}
    for r in refs:
        root = find(r)
        if root not in ids:
            ids[root] = len(ids)
        cluster_of[r] = ids[root]
    return cluster_of, gap


def classify_nets(board, cluster_of):
    """Split routable nets into intra-cluster (island) and inter-cluster
    (highway) by whether all their pads sit in one component cluster."""
    island, highway = [], []
    for net in board.nets.values():
        pads = board.pads_of_net(net)
        if len(pads) < 2:
            continue
        clusters = {cluster_of.get(p.ref) for p in pads}
        clusters.discard(None)
        (island if len(clusters) <= 1 else highway).append(net.name)
    return island, highway


def route_all_planned(router, progress=None):
    """Phased strategic routing (the veteran method):
      Phase 0  diff pairs (inner layers)
      Phase 1  intra-cluster ISLAND nets, flat on the component layers
      Phase 3  inter-cluster HIGHWAY nets, full layer/via freedom
    then the standard completion tail. (Phase 2 power/ground + aligned
    Phase-3 transitions come next.)"""
    from .diffpair import find_diff_pairs, route_diff_pair

    board = router.board
    sig = list(getattr(board, "signal_layers", None) or board.layers)
    component_layers = frozenset({sig[0], sig[-1]})  # Top + Bottom

    # Phase 0 — diff pairs first
    for net_p, net_n, gap in find_diff_pairs(board):
        if route_diff_pair(router, net_p, net_n, gap):
            router.result.diffpair_nets |= {net_p.name, net_n.name}

    cl, _ = component_clusters(board)
    island, highway = classify_nets(board, cl)
    dp = router.result.diffpair_nets
    island = [n for n in island if n not in dp]
    highway = [n for n in highway if n not in dp]
    if progress:
        progress(0, 0, f"planner: {len(island)} island nets (flat), "
                       f"{len(highway)} highway nets (free)", router.result)

    # Phase 1 — islands flat on component layers (keep inner layers open)
    # shortest first so the tightest local nets lock in cleanly
    def span(name):
        p = board.pads_of_net(board.nets[name])
        xs = [q.x for q in p]; ys = [q.y for q in p]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))
    for name in sorted(island, key=span):
        net = board.nets[name]
        net.pref_layers = component_layers
        try:
            router.route_net(net)
        finally:
            net.pref_layers = None

    # Phase 3 — highways with full freedom (longest last is fine; the tail
    # cleans up). Route shorter highways first, longest truly last.
    n = len(highway)
    for i, name in enumerate(sorted(highway, key=span)):
        router.board.nets[name].pref_layers = None
        router.route_net(board.nets[name])
        if progress and (i % 10 == 0 or i == n - 1):
            progress(i + 1, n, name, router.result)

    router._rip_and_retry(progress)
    router._shake_parallel(progress)
    router._endgame(progress)
    if router.result.failed:
        router._shake_parallel(progress)
    return router.result
