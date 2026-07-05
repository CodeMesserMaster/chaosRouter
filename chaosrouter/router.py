"""Router core: smart net ordering + Steiner-tree growth + multi-source A*.

Each net grows as a tree: pads join by routing to the nearest point of the
net's already-routed copper (Prim order), not to a fixed partner pad. All
routed copper is rasterized back into the workspace, so every later route
sees and respects everything routed before it.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np

from .fields import seg_clear, string_pull
from .grid import Workspace
from .model import Board, Net

SQRT2 = math.sqrt(2.0)
MOVES = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


@dataclass
class Trace:
    net: str
    layer: str
    coords: list  # [(x, y), ...]
    width: float
    no_fillet: bool = False  # already curved by construction (coupled pairs)


@dataclass
class Via:
    net: str
    x: float
    y: float
    diameter: float
    padstack: str = "Default"


@dataclass
class RouteResult:
    traces: list[Trace] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)
    routed_edges: int = 0
    diffpair_nets: set = field(default_factory=set)  # already curved+coupled
    edges_by_net: dict = field(default_factory=dict)  # for rip-up accounting
    # long pair legs whose COUPLED route failed: (p, n, gap, pid_a, pid_b) —
    # they must be fixed by pair-aware rip-up, never silently decoupled
    pair_segments_failed: list = field(default_factory=list)

    def traces_by_layer(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for t in self.traces:
            out.setdefault(t.layer, []).append((t.coords, t.width, t.net))
        return out


class Router:
    def __init__(
        self,
        board: Board,
        ws: Workspace,
        via_diameter: float = 19.68503,
        via_cost: float = 150.0,
        edt_margin: float = 1.0,
        power_sources: dict | None = None,
        strict_width: bool = False,
        avoid_padstacks: frozenset = frozenset(),
    ):
        self.board = board
        self.ws = ws
        self.via_diameter = via_diameter
        self.via_cost = via_cost
        self.edt_margin = edt_margin  # in grid-step units, covers quantization
        self.power_sources = power_sources or {}
        # strict_width: never route below class width (DipTrace's SES import
        # normalizes wire widths back to the class width, so any sub-width
        # neck would be force-widened into a clearance violation there)
        self.strict_width = strict_width
        # via padstacks the target CAD mishandles on SES import (DipTrace
        # blows up "inPadVia"); routing simply never places them
        self.avoid_padstacks = avoid_padstacks
        # pathfinder mode: per-layer fine-grid penalty attracting the
        # search toward a negotiated corridor (None = off)
        self._corridor_bias = None
        self.result = RouteResult()
        import threading

        self._result_lock = threading.Lock()

    # ---------------- net ordering -------------------------------------
    def net_order(self) -> list[Net]:
        """Shortest-MST nets first; giant nets naturally go last."""
        from scipy.sparse.csgraph import minimum_spanning_tree
        from scipy.spatial.distance import pdist, squareform

        planes = getattr(self.board, "plane_nets", frozenset())
        scored = []
        for net in self.board.nets.values():
            if net.name in planes:
                continue  # plane nets are poured, not routed as signal traces
            pads = self.board.pads_of_net(net)
            if len(pads) < 2:
                continue
            pts = np.array([(p.x, p.y) for p in pads])
            mst = minimum_spanning_tree(squareform(pdist(pts)))
            scored.append((mst.sum(), net))
        scored.sort(key=lambda t: t[0])
        return [net for _, net in scored]

    # ---------------- top level ----------------------------------------
    RIP_PASSES = 3
    MAX_VICTIM_EDGES = 30  # never rip nets bigger than this (GND, 5v...)
    MAX_RIPS_PER_NET = 2

    def route_all(
        self, progress=None, rip_up: bool = True, workers: int | None = None,
        persist_seconds: float = 0.0,
    ) -> RouteResult:
        import os

        from .diffpair import find_diff_pairs, route_diff_pair

        # differential pairs first: most constrained, must stay coupled
        for net_p, net_n, gap in find_diff_pairs(self.board):
            if route_diff_pair(self, net_p, net_n, gap):
                self.result.diffpair_nets |= {net_p.name, net_n.name}
            # on failure the nets fall through to normal individual routing

        nets = [n for n in self.net_order() if n.name not in self.result.diffpair_nets]
        if workers is None:
            # use (nearly) all logical cores — the numba kernels are nogil
            workers = max(1, (os.cpu_count() or 8) - 2)
        self._workers = workers
        if workers > 1:
            self._route_parallel(nets, workers, progress)
        else:
            for i, net in enumerate(nets):
                self.route_net(net)
                if progress:
                    progress(i + 1, len(nets), net.name, self.result)
        if rip_up:
            self._rip_and_retry(progress)
            self._shake_parallel(progress)   # parallel across cores
            self._endgame(progress)
            if self.result.failed:
                self._shake_parallel(progress)
            # persistence: the board is routable — keep rolling fresh seeds
            # through shake+endgame until complete or the budget runs out
            import time as _t

            t0 = _t.time()
            attempt = 1
            while (
                self.result.failed
                and persist_seconds > 0
                and _t.time() - t0 < persist_seconds
            ):
                attempt += 1
                if progress:
                    progress(
                        0, 0,
                        f"persist: attempt {attempt}, "
                        f"{len(self.result.failed)} failed, "
                        f"{persist_seconds - (_t.time() - t0):.0f}s left",
                        self.result,
                    )
                self._shake(progress, seed=20260703 + attempt * 7919)
                self._endgame(progress)
        return self.result

    def set_corridor_bias(self, bias):
        self._corridor_bias = bias

    # ---------------- stochastic shaker ---------------------------------
    SHAKE_ROUNDS = 60
    SHAKE_PATIENCE = 15  # stop after this many consecutive rejections

    def _shake(self, progress=None, seed: int = 20260703):
        """Monte-Carlo local perturbation: when deterministic rip-up stalls,
        rip a random small neighborhood around a failing edge and re-route
        in random order. Keep the new state only if total failures dropped;
        otherwise roll back exactly. Seeded: runs stay reproducible."""
        import random

        from shapely.geometry import box as shp_box

        rng = random.Random(seed)
        rounds = 0
        rejects = 0
        while rounds < self.SHAKE_ROUNDS and rejects < self.SHAKE_PATIENCE:
            fails = [
                f for f in self.result.failed
                if f[0] not in self.result.diffpair_nets
            ]
            if not fails:
                break
            rounds += 1
            if progress and rounds % 10 == 0:
                progress(
                    0, 0,
                    f"  shake heartbeat: round {rounds}, {len(fails)} failed, "
                    f"{rejects} rejects in a row",
                    self.result,
                )
            net_name, pid_a, pid_b = fails[rounds % len(fails)]
            pa = self.board.pads.get(pid_a)
            pb = self.board.pads.get(pid_b)
            if pa is None or pb is None:
                continue
            m = 150.0
            qbox = shp_box(
                min(pa.x, pb.x) - m, min(pa.y, pb.y) - m,
                max(pa.x, pb.x) + m, max(pa.y, pb.y) + m,
            )
            cands = set()
            for layer in self.ws.layers:
                tree, items = self.ws._tree(layer)
                if tree is None:
                    continue
                for idx in tree.query(qbox):
                    onet, geom, clr, kind, _raw, _r = items[int(idx)]
                    if kind == "pad" or onet is None:
                        continue
                    if onet == net_name or onet in self.result.diffpair_nets:
                        continue
                    if self._net_edge_count(onet) > self.MAX_VICTIM_EDGES:
                        continue
                    cands.add(onet)
            if not cands:
                continue
            chosen = rng.sample(sorted(cands), min(len(cands), rng.randint(1, 3)))
            group = [net_name] + chosen

            snap_traces = [t for t in self.result.traces if t.net in group]
            snap_vias = [v for v in self.result.vias if v.net in group]
            snap_failed = list(self.result.failed)
            snap_edges = {g: self.result.edges_by_net.get(g, 0) for g in group}
            snap_count = self.result.routed_edges
            baseline = len(self.result.failed)

            for g in group:
                self._rip_net(g)
            order = group[:]
            rng.shuffle(order)
            for g in order:
                self.route_net(self.board.nets[g])

            if len(self.result.failed) < baseline:
                rejects = 0
                if progress:
                    progress(
                        0, 0,
                        f"  shake #{rounds}: {'+'.join(group)} -> "
                        f"{len(self.result.failed)} failed",
                        self.result,
                    )
                continue  # accepted
            rejects += 1

            # rejected: exact rollback
            for g in group:
                self._rip_net(g)
            for t in snap_traces:
                self.result.traces.append(t)
                self.ws.add_trace(t.net, t.layer, t.coords, t.width)
            for v in snap_vias:
                self.result.vias.append(v)
                self.ws.add_via(v.net, v.x, v.y, v.diameter)
            self.result.failed = snap_failed
            for g, c in snap_edges.items():
                if c:
                    self.result.edges_by_net[g] = c
            self.result.routed_edges = snap_count

    # ---------------- parallel shaker ------------------------------------
    def _shake_one(self, edge, seed):
        """One shake attempt around a failing edge, on its own disjoint
        region. Returns True if it reduced failures (kept), else rolls back.
        Thread-safe: touches only nets in its region; result/ws mutations
        are locked."""
        import random
        from shapely.geometry import box as shp_box

        rng = random.Random(seed)
        net_name, pid_a, pid_b = edge
        pa = self.board.pads.get(pid_a)
        pb = self.board.pads.get(pid_b)
        if pa is None or pb is None:
            return False
        m = 150.0
        qbox = shp_box(min(pa.x, pb.x) - m, min(pa.y, pb.y) - m,
                       max(pa.x, pb.x) + m, max(pa.y, pb.y) + m)
        cands = set()
        for layer in self.ws.layers:
            tree, items = self.ws._tree(layer)
            if tree is None:
                continue
            for idx in tree.query(qbox):
                onet, geom, clr, kind, _raw, _r = items[int(idx)]
                if kind == "pad" or onet is None:
                    continue
                if onet == net_name or onet in self.result.diffpair_nets:
                    continue
                if self._net_edge_count(onet) > self.MAX_VICTIM_EDGES:
                    continue
                cands.add(onet)
        if not cands:
            return False
        chosen = rng.sample(sorted(cands), min(len(cands), rng.randint(1, 3)))
        group = [net_name] + chosen

        with self._result_lock:
            snap_traces = [t for t in self.result.traces if t.net in group]
            snap_vias = [v for v in self.result.vias if v.net in group]
            snap_failed_group = [f for f in self.result.failed if f[0] in group]
            snap_edges = {g: self.result.edges_by_net.get(g, 0) for g in group}
        base_fail = len(snap_failed_group)

        for g in group:
            self._rip_net(g)
        order = group[:]
        rng.shuffle(order)
        for g in order:
            self.route_net(self.board.nets[g])

        now_fail = len([f for f in self.result.failed if f[0] in group])
        if now_fail < base_fail:
            return True
        # reject: exact rollback of just this group
        for g in group:
            self._rip_net(g)
        with self._result_lock:
            for t in snap_traces:
                self.result.traces.append(t)
                self.ws.add_trace(t.net, t.layer, t.coords, t.width)
            for v in snap_vias:
                self.result.vias.append(v)
                self.ws.add_via(v.net, v.x, v.y, v.diameter)
            self.result.failed.extend(snap_failed_group)
            for g, c in snap_edges.items():
                if c:
                    self.result.edges_by_net[g] = c
                    self.result.routed_edges += c
        return False

    def _disjoint_batches(self, edges, reach=650.0):
        """Group failing edges so that within a batch no two edges' regions
        overlap (reach = query box + reroute margin) — those can shake in
        parallel without touching each other's copper."""
        boxes = []
        for e in edges:
            pa = self.board.pads.get(e[1]); pb = self.board.pads.get(e[2])
            if pa is None or pb is None:
                continue
            boxes.append((e, (min(pa.x, pb.x) - reach, min(pa.y, pb.y) - reach,
                              max(pa.x, pb.x) + reach, max(pa.y, pb.y) + reach)))
        batches = []
        for e, b in boxes:
            placed = False
            for batch in batches:
                if all(not (b[0] < ob[2] and ob[0] < b[2]
                            and b[1] < ob[3] and ob[1] < b[3])
                       for _, ob in batch):
                    batch.append((e, b)); placed = True; break
            if not placed:
                batches.append([(e, b)])
        return [[e for e, _ in batch] for batch in batches]

    def _shake_parallel(self, progress=None, seed: int = 20260703, rounds=40):
        """Parallel shaker: each round, failing edges are packed into
        spatially-disjoint batches and every edge in a batch is shaken
        concurrently across all workers. Correct because disjoint regions
        never touch the same copper; quality identical to sequential (each
        attempt keeps only on strict local improvement)."""
        import concurrent.futures as cf

        workers = getattr(self, "_workers", 8)
        rejects = 0
        for r in range(rounds):
            fails = [f for f in self.result.failed
                     if f[0] not in self.result.diffpair_nets]
            if not fails:
                break
            batch = self._disjoint_batches(fails)[0]  # largest disjoint set
            with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                got = list(pool.map(
                    lambda e, i=[0]: self._shake_one(
                        e, seed + r * 104729 + hash(e[0]) % 9973),
                    batch))
            improved = sum(got)
            if progress and (r % 5 == 0 or improved):
                progress(0, 0,
                         f"  pshake round {r+1}: {len(batch)} parallel, "
                         f"+{improved} fixed, {len(self.result.failed)} left",
                         self.result)
            rejects = 0 if improved else rejects + 1
            if rejects >= 12:
                break

    # ---------------- endgame --------------------------------------------
    ENDGAME_WINDOWS = (200.0, 350.0)  # eviction radius escalation, mil

    def _finish_edge(self, net: Net, pad, near_pad) -> bool:
        """Surgically route ONE missing pad into the net's existing tree
        without disturbing the rest of the net (used for big nets whose
        full rebuild is too risky a transaction)."""
        ws = self.ws
        target = {ly: np.zeros((ws.ny, ws.nx), dtype=bool) for ly in ws.layers}
        target_centers: list[tuple[int, int, int]] = []
        failed_pads = {
            p
            for nm, a, b in self.result.failed
            for p in (a, b)
            if nm == net.name
        }
        # tree copper = every registered wire/via of this net
        for layer in ws.layers:
            _, items = ws._tree(layer)
            for onet, geom, clr, kind, raw, r in items:
                if onet != net.name or kind == "pad":
                    continue
                iys, ixs = ws._cells_in_geom(geom, grow=0)
                target[layer][iys, ixs] = True
        # plus connected pads (failed pads never become targets)
        for p2 in self.board.pads_of_net(net):
            if p2.pin_id == pad.pin_id or p2.pin_id in failed_pads:
                continue
            self._add_pad_to_target(p2, target, target_centers)
        if not target_centers:
            return False
        dist = ws.foreign_distance(net.name)
        via_name, via_dia = self.via_for(net)
        return self._connect_pad(
            net, [pad, near_pad], 0, 1, dist, target, target_centers,
            (150.0, 500.0, 1e9), None, via_name, via_dia, record_fail=False,
        )

    def _anypath(self, progress=None, rounds=2):
        """ABSOLUTE last resort — 'just get it connected somehow'. Drops EVERY
        structural constraint (Manhattan grain, layer preferences, orthogonal-
        only, per-layer base costs) and makes vias near-free, then runs the
        eviction endgame so the remaining edges can take ANY legal path through
        the whole board on any layers with as many vias as needed. Ugly is
        fine; a connected board beats a pretty open one. Restores all settings
        after, so only the still-failed edges are touched."""
        if not self.result.failed:
            return
        saved = (
            getattr(self, "_grain", None),
            getattr(self, "_layer_base", None),
            getattr(self, "_orthogonal", False),
            self.via_cost,
        )
        self._grain = None
        self._layer_base = None
        self._orthogonal = False
        self.via_cost = 20.0  # vias almost free: trade vias for connectivity
        try:
            for _ in range(rounds):
                before = len(self.result.failed)
                if not before:
                    break
                self._rip_and_retry(progress)
                self._endgame(progress)
                if progress:
                    progress(0, 0,
                             f"any-path: {before} -> {len(self.result.failed)} left",
                             self.result)
                if len(self.result.failed) == before:
                    break
        finally:
            (self._grain, self._layer_base,
             self._orthogonal, self.via_cost) = saved

    def _endgame(self, progress=None):
        """Deterministic last resort for edges that survive rip-up and
        shaking: evict EVERY rippable net crossing the edge's neighborhood,
        give the failing net first pick of the freed space, then re-route
        the evicted nets around it (smallest first). Strict-improvement
        accept with exact rollback. Escalates the eviction radius; on the
        final radius even big nets (GND, power) may be evicted."""
        from shapely.geometry import box as shp_box

        for round_i, m in enumerate(self.ENDGAME_WINDOWS):
            last = round_i == len(self.ENDGAME_WINDOWS) - 1
            for edge in list(self.result.failed):
                if edge not in self.result.failed:
                    continue  # already fixed as a bystander of an eviction
                net_name, pid_a, pid_b = edge
                if net_name in self.result.diffpair_nets:
                    continue
                pa = self.board.pads.get(pid_a)
                pb = self.board.pads.get(pid_b)
                if pa is None or pb is None:
                    continue
                qbox = shp_box(
                    min(pa.x, pb.x) - m, min(pa.y, pb.y) - m,
                    max(pa.x, pb.x) + m, max(pa.y, pb.y) + m,
                )
                # victims ranked by how close their copper comes to the
                # direct pad-to-pad line (the physical seal moves first),
                # then by size; cap AFTER ranking so a big sealing net
                # (fat power trace) is never dropped
                from shapely.geometry import LineString as _LS

                seg = _LS([(pa.x, pa.y), (pb.x, pb.y)])
                seal_d: dict[str, float] = {}
                for layer in self.ws.layers:
                    tree, items = self.ws._tree(layer)
                    if tree is None:
                        continue
                    for idx in tree.query(qbox):
                        onet, geom, clr, kind, _raw, _r = items[int(idx)]
                        if kind == "pad" or onet is None:
                            continue
                        if onet == net_name or onet in self.result.diffpair_nets:
                            continue
                        if (
                            not last
                            and self._net_edge_count(onet) > self.MAX_VICTIM_EDGES
                        ):
                            continue
                        d = geom.distance(seg)
                        if d < seal_d.get(onet, 1e9):
                            seal_d[onet] = d
                victims = set(seal_d)
                ordered = sorted(
                    victims, key=lambda n: (seal_d[n], self._net_edge_count(n))
                )[:25]
                group = [net_name] + ordered

                snap_traces = [t for t in self.result.traces if t.net in group]
                snap_vias = [v for v in self.result.vias if v.net in group]
                snap_failed = list(self.result.failed)
                snap_edges = {
                    g: self.result.edges_by_net.get(g, 0) for g in group
                }
                snap_count = self.result.routed_edges
                baseline = len(self.result.failed)

                # big failing nets are NOT rebuilt (too risky a transaction):
                # keep their tree, evict the seal, stitch just the edge.
                # Evictions are INCREMENTAL — closest seal first, next one
                # only if the stitch still fails — so the re-route
                # disturbance stays as small as possible.
                big = self._net_edge_count(net_name) > self.MAX_VICTIM_EDGES
                old_via_cost = self.via_cost
                self.via_cost = 50.0
                fixed = False
                ripped: list[str] = []
                try:
                    if not big:
                        self._rip_net(net_name)
                    for v in ordered:
                        self._rip_net(v)
                        ripped.append(v)
                        if big:
                            fixed = self._finish_edge(
                                self.board.nets[net_name], pa, pb
                            )
                            if fixed and edge in self.result.failed:
                                self.result.failed.remove(edge)
                        else:
                            self.route_net(self.board.nets[net_name])
                            fixed = edge not in self.result.failed
                            if not fixed:
                                self._rip_net(net_name)  # clean partial tree
                        if fixed:
                            break
                    if not big and not fixed:
                        self.route_net(self.board.nets[net_name])
                finally:
                    self.via_cost = old_via_cost
                for g in ripped:
                    self.route_net(self.board.nets[g])

                target_fixed = edge not in self.result.failed
                if len(self.result.failed) < baseline or (
                    target_fixed and len(self.result.failed) == baseline
                ):
                    # strict improvement, or an equal trade that resolves
                    # THIS proven-stuck edge (the new failure gets its own
                    # endgame/shake attempts afterwards)
                    if progress:
                        progress(
                            0, 0,
                            f"  endgame {net_name} (r={m:.0f}, "
                            f"{len(ripped)} evicted) -> "
                            f"{len(self.result.failed)} failed",
                            self.result,
                        )
                    continue  # accepted
                # rejected: exact rollback
                for g in group:
                    self._rip_net(g)
                for t in snap_traces:
                    self.result.traces.append(t)
                    self.ws.add_trace(t.net, t.layer, t.coords, t.width)
                for v in snap_vias:
                    self.result.vias.append(v)
                    self.ws.add_via(v.net, v.x, v.y, v.diameter)
                self.result.failed = snap_failed
                for g, c in snap_edges.items():
                    if c:
                        self.result.edges_by_net[g] = c
                self.result.routed_edges = snap_count
            if not self.result.failed:
                break

    # ---------------- parallel scheduling --------------------------------
    PARALLEL_WINDOWS = (150.0, 500.0)  # capped so regions stay disjoint
    REGION_MARGIN = 550.0  # > max parallel window: no two batched nets interact

    def _net_region(self, net):
        pads = self.board.pads_of_net(net)
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        m = self.REGION_MARGIN
        return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)

    def _route_parallel(self, nets, workers, progress):
        """Route spatially disjoint nets concurrently. Nets too large to
        batch (or whose capped-window attempt fails) route sequentially;
        the rip-up pass retries any remaining failures with full windows."""
        import concurrent.futures as cf

        minx, miny, maxx, maxy = self.board.outline.bounds
        bw, bh = maxx - minx, maxy - miny

        def is_big(net):
            r = self._net_region(net)
            return (
                self._net_edge_count(net.name) > 40
                or (r[2] - r[0]) > 0.6 * bw
                or (r[3] - r[1]) > 0.6 * bh
            )

        small = [n for n in nets if not is_big(n)]
        large = [n for n in nets if is_big(n)]
        total = len(nets)
        done = 0

        self.ws.pad_distance()  # prime the shared cache before threading

        pending = list(small)
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            while pending:
                batch, regions, rest = [], [], []
                for net in pending:
                    r = self._net_region(net)
                    if len(batch) < workers and all(
                        r[2] < q[0] or q[2] < r[0] or r[3] < q[1] or q[3] < r[1]
                        for q in regions
                    ):
                        batch.append(net)
                        regions.append(r)
                    else:
                        rest.append(net)
                pending = rest
                futs = {
                    pool.submit(self.route_net, net, self.PARALLEL_WINDOWS): net
                    for net in batch
                }
                for fut in cf.as_completed(futs):
                    fut.result()  # propagate worker exceptions
                    done += 1
                    if progress:
                        progress(done, total, futs[fut].name, self.result)

        for net in large:
            self.route_net(net)
            done += 1
            if progress:
                progress(done, total, net.name, self.result)

    # ---------------- rip-up and retry ----------------------------------
    def _net_edge_count(self, net_name: str) -> int:
        net = self.board.nets.get(net_name)
        return max(0, len(net.pad_ids) - 1) if net else 999

    def _retry_failed_pairs(self, pass_i, rip_counts, progress):
        """Pair-aware rip-up: rip what blocks a failed coupled leg, then
        re-route the whole pair coupled. Pairs never decouple silently."""
        from .diffpair import route_diff_pair

        queued = list(self.result.pair_segments_failed)
        if not queued:
            return
        self.result.pair_segments_failed = []
        pairs: dict[tuple, list] = {}
        for p_name, n_name, gap, pid_a, pid_b in queued:
            pairs.setdefault((p_name, n_name, gap), []).append((pid_a, pid_b))

        size_cap = self.MAX_VICTIM_EDGES if pass_i < self.RIP_PASSES - 1 else 10**9
        for (p_name, n_name, gap), legs in pairs.items():
            net_p = self.board.nets[p_name]
            net_n = self.board.nets[n_name]
            victims: set[str] = set()
            for pid_a, pid_b in legs:
                pad_a = self.board.pads.get(pid_a)
                pad_b = self.board.pads.get(pid_b)
                if pad_a and pad_b:
                    victims |= self._diagnose_blockers(net_p, pad_a, pad_b)
            victims = {
                v
                for v in victims
                if v not in self.result.diffpair_nets
                and self._net_edge_count(v) <= size_cap
                and rip_counts.get(v, 0) < self.MAX_RIPS_PER_NET
            }
            for v in sorted(victims):
                rip_counts[v] = rip_counts.get(v, 0) + 1
                self._rip_net(v)
            self._rip_net(p_name)
            self._rip_net(n_name)
            route_diff_pair(self, net_p, net_n, gap)
            for v in sorted(victims):
                self.route_net(self.board.nets[v])
            if progress:
                coupled = not any(
                    q[0] == p_name for q in self.result.pair_segments_failed
                )
                progress(
                    0, 0,
                    f"  pair {p_name}/{n_name}: ripped {len(victims)} blockers -> "
                    f"{'COUPLED' if coupled else 'still failing'}",
                    self.result,
                )

    def _rip_and_retry(self, progress=None):
        rip_counts: dict[str, int] = {}
        unfixable: set[str] = set()
        for pass_i in range(self.RIP_PASSES):
            self._retry_failed_pairs(pass_i, rip_counts, progress)
            failing = []
            for net_name, *_ in self.result.failed:
                if net_name in self.result.diffpair_nets or net_name in unfixable:
                    continue  # never decouple a pair; skip proven-stuck nets
                if net_name not in failing:
                    failing.append(net_name)
            if not failing:
                break
            if progress:
                progress(0, 0, f"rip-up pass {pass_i + 1}: {len(failing)} nets", self.result)

            for net_name in failing:
                net = self.board.nets[net_name]
                edges = [f for f in self.result.failed if f[0] == net_name]
                victims: set[str] = set()
                for _, pid_a, pid_b in edges:
                    pad_a = self.board.pads.get(pid_a)
                    pad_b = self.board.pads.get(pid_b)
                    if pad_a is None or pad_b is None:
                        continue
                    victims |= self._diagnose_blockers(net, pad_a, pad_b)
                # big nets (GND/5v) are protected early; in the final pass
                # they become rippable too — they re-route quickly and are
                # often the only thing left standing in the way
                size_cap = (
                    self.MAX_VICTIM_EDGES if pass_i < self.RIP_PASSES - 1 else 10**9
                )
                raw = len(victims)
                victims = {
                    v
                    for v in victims
                    if self._net_edge_count(v) <= size_cap
                    and rip_counts.get(v, 0) < self.MAX_RIPS_PER_NET
                }
                if progress and raw != len(victims):
                    progress(
                        0, 0,
                        f"  {net_name}: {raw} blockers found, {len(victims)} rippable",
                        self.result,
                    )
                # differential-pair victims rip and re-route as whole pairs
                pair_victims = []
                if victims & self.result.diffpair_nets:
                    from .diffpair import find_diff_pairs

                    for np_, nn_, gap in find_diff_pairs(self.board):
                        if np_.name in victims or nn_.name in victims:
                            pair_victims.append((np_, nn_, gap))
                            victims -= {np_.name, nn_.name}

                # rip the failing net itself plus the blockers, reroute all
                before = {tuple(f[1:]) for f in edges}
                ripped = [net_name] + sorted(victims)
                for v in ripped:
                    rip_counts[v] = rip_counts.get(v, 0) + 1
                    self._rip_net(v)
                for np_, nn_, gap in pair_victims:
                    rip_counts[np_.name] = rip_counts.get(np_.name, 0) + 1
                    self._rip_net(np_.name)
                    self._rip_net(nn_.name)
                # pairs re-route FIRST (rigid, must stay coupled — and with
                # module avoidance they pick out-of-the-way corridors), then
                # the failing net and other victims flow around them
                from .diffpair import route_diff_pair

                for np_, nn_, gap in pair_victims:
                    route_diff_pair(self, np_, nn_, gap)
                for v in ripped:
                    self.route_net(self.board.nets[v])
                after = {
                    tuple(f[1:]) for f in self.result.failed if f[0] == net_name
                }
                if not victims and after and after >= before:
                    # nothing to rip and nothing improved: stop retrying this net
                    unfixable.add(net_name)
                if progress:
                    progress(
                        0, 0,
                        f"  ripped {net_name} + {len(victims)} blockers "
                        f"-> failed now {len(self.result.failed)}",
                        self.result,
                    )
        # pairs broken during the final pass still get their retry
        self._retry_failed_pairs(self.RIP_PASSES - 1, rip_counts, progress)

    def _rip_net(self, net_name: str):
        with self._result_lock:
            self.result.traces = [t for t in self.result.traces if t.net != net_name]
            self.result.vias = [v for v in self.result.vias if v.net != net_name]
            self.result.failed = [f for f in self.result.failed if f[0] != net_name]
            self.result.routed_edges -= self.result.edges_by_net.pop(net_name, 0)
        self.ws.remove_net_wiring(net_name)

    def _diagnose_blockers(self, net: Net, pad_a, pad_b) -> set[str]:
        """Relaxed A*: foreign traces/vias may be crossed at a penalty scaled
        by how expensive that net is to re-route. The nets crossed on the
        cheapest path are the recommended victims.

        Searches at NECK width — the most permissive envelope the router
        itself may use — so blockage by pads doesn't mask rippable traces."""
        ws = self.ws
        step = ws.step
        req = self.neck_width(net) / 2 + self.neck_gap(net) + self.OPT_MARGIN * step

        ax, ay = ws.to_cell(pad_a.x, pad_a.y)
        bx, by = ws.to_cell(pad_b.x, pad_b.y)
        span = math.hypot(pad_a.x - pad_b.x, pad_a.y - pad_b.y)
        m = int(max(400.0, 0.4 * span) / step)
        x0, x1 = max(0, min(ax, bx) - m), min(ws.nx - 1, max(ax, bx) + m)
        y0, y1 = max(0, min(ay, by) - m), min(ws.ny - 1, max(ay, by) + m)
        wx, wy = x1 - x0 + 1, y1 - y0 + 1
        nl = len(ws.layers)
        window = (x0, y0, x1, y1)

        from scipy.ndimage import distance_transform_edt as _edt_idx

        from .grid import BLOCK

        nid_self = ws.net_index[net.name]
        own = ws.own_exempt_mask(net.name, self.neck_width(net))
        trav = np.empty((nl, wy, wx), dtype=bool)
        overlay = np.full((nl, wy, wx), -1, dtype=np.int32)
        for li, layer in enumerate(ws.layers):
            sub_owner = ws.owner[layer][y0 : y1 + 1, x0 : x1 + 1]
            sub_wire = ws.wiring_owner[layer][y0 : y1 + 1, x0 : x1 + 1]
            foreign = (sub_owner == BLOCK) | ((sub_owner >= 0) & (sub_owner != nid_self))
            d, (niy, nix) = _edt_idx(~foreign, return_indices=True)
            trav[li] = (d * step >= req) | own[layer][y0 : y1 + 1, x0 : x1 + 1]
            # attribute each blocked cell to the WIRING that blocks it (via
            # the nearest-obstacle feature transform): rippable traces/vias
            # become crossable; pads/board stay hard walls
            near_wire = sub_wire[niy, nix]
            within = d * step < req + 0.5 * step
            overlay[li] = np.where(
                within & (near_wire >= 0) & (near_wire != nid_self), near_wire, -1
            )

        penalty = {}
        idx_to_net = {v: k for k, v in ws.net_index.items()}
        for nid in np.unique(overlay[overlay >= 0]):
            other = idx_to_net[int(nid)]
            if other in self.result.diffpair_nets:
                penalty[int(nid)] = step * 80.0  # victim of last resort
            else:
                cost_edges = min(self._net_edge_count(other), 40)
                penalty[int(nid)] = step * (5.0 + 0.5 * cost_edges)

        starts, s_centers = self._pad_cells(pad_a, window)
        goals, g_centers = self._pad_cells(pad_b, window)
        starts += s_centers
        goals += g_centers
        if not starts or not goals:
            return set()
        for li, iy, ix in s_centers + g_centers:
            trav[li, iy, ix] = True
        goal_mask = np.zeros((nl, wy, wx), dtype=bool)
        for li, iy, ix in goals:
            goal_mask[li, iy, ix] = True

        from .astar_kernel import dijkstra_cost

        layer_stride = wy * wx
        over_flat = overlay.reshape(-1)
        trav_flat = trav.reshape(-1) | (over_flat >= 0)
        cost = np.zeros(nl * layer_stride, dtype=np.float32)
        if penalty:
            lut = np.zeros(max(penalty) + 2, dtype=np.float32)
            for nid, pen in penalty.items():
                lut[nid] = pen
            valid = over_flat >= 0
            cost[valid] = lut[over_flat[valid]]
        start_states = np.array(
            sorted({li * layer_stride + iy * wx + ix for li, iy, ix in starts}),
            dtype=np.int64,
        )
        min_via = self.via_min_for(net)[1]
        via_ok = (
            ws.pad_distance()[y0 : y1 + 1, x0 : x1 + 1] >= min_via / 2 + 1.0
        ).reshape(-1)
        found, parent = dijkstra_cost(
            trav_flat, goal_mask.reshape(-1), cost, via_ok, start_states,
            nl, wy, wx, step, self.via_cost,
        )
        if found < 0:
            return set()
        victims = set()
        idx_to_net = {v: k for k, v in ws.net_index.items()}
        s = found
        while s >= 0:
            nid = over_flat[s]
            if nid >= 0:
                victims.add(idx_to_net[nid])
            s = parent[s]
        return victims

    def route_net(self, net: Net, windows=(150.0, 500.0, 1e9)):
        ws = self.ws
        pads = self.board.pads_of_net(net)
        if len(pads) < 2:
            return

        via_name, via_dia = self.via_for(net)
        pts = np.array([(p.x, p.y) for p in pads])
        dmat = np.hypot(pts[:, 0, None] - pts[None, :, 0], pts[:, 1, None] - pts[None, :, 1])

        # local nets get windowed distance fields (~10x cheaper) and capped
        # search windows; big/spread nets pay for the full board once
        span_x = pts[:, 0].max() - pts[:, 0].min()
        span_y = pts[:, 1].max() - pts[:, 1].min()
        if span_x > 1500 or span_y > 1500 or len(pads) > 40:
            bounds = None
        else:
            m = 620.0
            bounds = (
                pts[:, 0].min() - m, pts[:, 1].min() - m,
                pts[:, 0].max() + m, pts[:, 1].max() + m,
            )
        dist = ws.foreign_distance(net.name, bounds)
        # cheap windowed attempts first; a failing edge gets ONE full-board
        # retry so the last-resort detour guarantee is never lost
        local_windows = tuple(w for w in windows if w <= 500.0) or (150.0, 500.0)
        full_retry = 1e9 in windows or any(w > 500.0 for w in windows)
        full_dist_cache: dict = {}

        def full_dist():
            if "d" not in full_dist_cache:
                full_dist_cache["d"] = ws.foreign_distance(net.name)
            return full_dist_cache["d"]

        # Prim order. Signal nets seed at the most central pad; power nets
        # seed at the source (user-specified pin, else the largest pad =
        # likely regulator output / bulk cap) so branches grow star-like
        # outward from the source instead of daisy-chaining arbitrarily.
        start = self._seed_index(net, pads, dmat)
        connected = [start]
        remaining = set(range(len(pads))) - {start}

        # target mask = copper belonging to the connected tree
        target = {layer: np.zeros((ws.ny, ws.nx), dtype=bool) for layer in ws.layers}
        target_centers: list[tuple[int, int, int]] = []
        self._add_pad_to_target(pads[start], target, target_centers)

        # Big nets (GND, 5v): connections whose search regions don't overlap
        # attach to the SAME tree independently — route them concurrently.
        import threading

        can_par = (
            len(pads) > 40
            and getattr(self, "_workers", 1) > 1
            and threading.current_thread() is threading.main_thread()
        )
        if not can_par:
            while remaining:
                j = min(remaining, key=lambda k: min(dmat[k][c] for c in connected))
                near_i = min(connected, key=lambda c: dmat[j][c])
                remaining.discard(j)
                ok = self._connect_pad(
                    net, pads, j, near_i, dist, target, target_centers,
                    local_windows, bounds, via_name, via_dia, record_fail=False,
                )
                if not ok and full_retry:
                    ok = self._connect_pad(
                        net, pads, j, near_i, full_dist(), target, target_centers,
                        windows, None, via_name, via_dia,
                    )
                elif not ok:
                    with self._result_lock:
                        self.result.failed.append(
                            (net.name, pads[j].pin_id, pads[near_i].pin_id)
                        )
                connected.append(j)
                if ok:  # failed pads never become targets (island stubs)
                    self._add_pad_to_target(pads[j], target, target_centers)
        else:
            import concurrent.futures as cf

            workers = self._workers
            wmax = max(local_windows)
            deferred = []  # edges needing the full-window sequential retry
            with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                while remaining:
                    order = sorted(
                        remaining, key=lambda k: min(dmat[k][c] for c in connected)
                    )
                    batch, regions = [], []
                    for j in order:
                        if len(batch) >= workers:
                            break
                        near_i = min(connected, key=lambda c: dmat[j][c])
                        m = wmax + 60.0
                        r = (
                            min(pts[j][0], pts[near_i][0]) - m,
                            min(pts[j][1], pts[near_i][1]) - m,
                            max(pts[j][0], pts[near_i][0]) + m,
                            max(pts[j][1], pts[near_i][1]) + m,
                        )
                        if all(
                            r[2] < q[0] or q[2] < r[0] or r[3] < q[1] or q[3] < r[1]
                            for q in regions
                        ):
                            batch.append((j, near_i))
                            regions.append(r)
                    for j, _ in batch:
                        remaining.discard(j)
                    futs = [
                        (
                            j,
                            near_i,
                            pool.submit(
                                self._connect_pad, net, pads, j, near_i, dist,
                                target, target_centers, local_windows, bounds,
                                via_name, via_dia, False,
                            ),
                        )
                        for j, near_i in batch
                    ]
                    for j, near_i, f in futs:
                        ok = f.result()
                        connected.append(j)
                        if ok:  # failed pads never become targets
                            self._add_pad_to_target(pads[j], target, target_centers)
                        else:
                            deferred.append((j, near_i))
            # stubborn edges get the sequential full-window last resort
            for j, near_i in deferred:
                ok = self._connect_pad(
                    net, pads, j, near_i, full_dist() if full_retry else dist,
                    target, target_centers, windows, None,
                    via_name, via_dia,
                )
                if ok:
                    self._add_pad_to_target(pads[j], target, target_centers)
        ws.drop_exempt(net.name)  # free this net's cached masks

    def _connect_pad(
        self, net, pads, j, near_i, dist, target, target_centers,
        windows, bounds, via_name, via_dia, record_fail: bool = True,
    ) -> bool:
        """Route one pad into the net's tree (thread-safe)."""
        ws = self.ws
        # retry ladder: preferred via -> pin neck-down -> smallest via
        # -> whole connection at neck width (trapped pins, e.g. inside
        #    a module's pad field)
        attempts = [("full", via_name, via_dia)]
        min_name, min_dia = self.via_min_for(net)
        # pin taper never necks below the connecting pad's copper width;
        # the whole-hop narrow fallback may go to class neck width as a
        # true last resort (an unrouted net is worse than a short pinch —
        # the taper/fatten passes restore width wherever clearance allows)
        floor_w = self._floor_for(net, pads[j])
        neck_w = self.neck_width(net)
        if floor_w < net.width - 1e-6:
            attempts.append(("neck", via_name, via_dia))
            if min_dia < via_dia - 1e-6:
                attempts.append(("neck", min_name, min_dia))
        if neck_w < net.width - 1e-6:
            attempts.append(("narrow", min_name, min_dia))
        if len(attempts) == 1 and min_dia < via_dia - 1e-6:
            attempts.append(("full", min_name, min_dia))
        out = None
        use_via = (via_name, via_dia)
        for mode, vn, vd in attempts:
            net_view = self._narrow_view(net, neck_w) if mode == "narrow" else net
            out = self._route_to_tree(
                net_view, pads[j], (pads[near_i].x, pads[near_i].y),
                target, target_centers, dist,
                ws.own_exempt_mask(net.name, net_view.width, bounds),
                windows=windows, via_dia=vd, neck=(mode == "neck"),
                bounds=bounds,
                floor_w=(neck_w if mode == "narrow" else floor_w),
            )
            if out is not None:
                use_via = (vn, vd)
                break
        if out is None:
            if record_fail:
                with self._result_lock:
                    self.result.failed.append(
                        (net.name, pads[j].pin_id, pads[near_i].pin_id)
                    )
            # the pad joins Prim ordering but NOT the target: later
            # connections must never attach to a disconnected island
            return False

        runs, vias = out
        for layer, coords, width in runs:
            if len(coords) >= 2:
                # graded restore: taper piecewise so only the actual pinch
                # stays narrow; everywhere else recovers width in 0.1mm steps
                if width < net.width - 1e-6:
                    pieces = self._taper_runs(net, layer, coords, width)
                else:
                    pieces = [(coords, width)]
                for pcoords, pwidth in pieces:
                    with self._result_lock:
                        self.result.traces.append(
                            Trace(net.name, layer, pcoords, pwidth)
                        )
                    ws.add_trace(net.name, layer, pcoords, pwidth)
                iys, ixs = ws.line_cells(coords)
                target[layer][iys, ixs] = True
        for x, y in vias:
            with self._result_lock:
                self.result.vias.append(
                    Via(net.name, x, y, use_via[1], padstack=use_via[0])
                )
            ws.add_via(net.name, x, y, use_via[1])
            # the whole via barrel is a connection target on every layer
            # (its center alone is often untraversable next to pads)
            from shapely.geometry import Point as _P

            iys, ixs = ws._cells_in_geom(_P(x, y).buffer(use_via[1] / 2), grow=0)
            for layer in ws.layers:
                target[layer][iys, ixs] = True
        with self._result_lock:
            self.result.routed_edges += 1
            self.result.edges_by_net[net.name] = (
                self.result.edges_by_net.get(net.name, 0) + 1
            )
        return True

    def via_min_for(self, net: Net) -> tuple[str, float]:
        """Smallest class-allowed via (fallback for congested areas)."""
        allowed = (
            net.net_class.use_vias
            if net.net_class and net.net_class.use_vias
            else self.board.via_padstacks
        )
        best = None
        for name in allowed:
            if name in self.avoid_padstacks:
                continue
            ps = self.board.padstacks.get(name)
            if not ps or not ps.shapes:
                continue
            b = ps.shapes[0].geometry.bounds
            dia = max(b[2] - b[0], b[3] - b[1])
            if best is None or dia < best[1]:
                best = (name, dia)
        return best or ("Default", self.via_diameter)

    def via_for(self, net: Net) -> tuple[str, float]:
        """Pick the net's via: smallest class-allowed padstack at least as
        wide as the trace, so power nets get power vias. 'inPadVia' is never
        chosen (vias in pads are forbidden)."""
        allowed = (
            net.net_class.use_vias
            if net.net_class and net.net_class.use_vias
            else self.board.via_padstacks
        )
        best = None
        fallback = ("Default", self.via_diameter)
        for name in allowed:
            if name == "inPadVia":
                continue
            ps = self.board.padstacks.get(name)
            if not ps or not ps.shapes:
                continue
            b = ps.shapes[0].geometry.bounds
            dia = max(b[2] - b[0], b[3] - b[1])
            if dia >= net.width and (best is None or dia < best[1]):
                best = (name, dia)
            if dia > fallback[1]:
                fallback = (name, dia)
        return best or fallback

    def _narrow_view(self, net: Net, width: float):
        """A view of the net at a reduced width, for last-resort narrow
        routing (never below the connecting pad's copper width)."""
        import types

        return types.SimpleNamespace(
            name=net.name,
            width=width,
            clearance=net.clearance,
            net_class=net.net_class,
            pad_ids=net.pad_ids,
        )

    def _seed_index(self, net, pads, dmat) -> int:
        src = self.power_sources.get(net.name)
        if src:
            for i, p in enumerate(pads):
                if p.pin_id == src:
                    return i
        if net.width > self.board.default_width + 1e-6:
            areas = [
                sum(s.geometry.area for s in p.padstack.shapes) for p in pads
            ]
            return int(np.argmax(areas))
        return int(np.argmin(dmat.sum(axis=1)))

    def _add_pad_to_target(self, pad, target, target_centers):
        ws = self.ws
        esc = getattr(self, "_escape", None)
        if esc and pad.pin_id in esc:
            # fanned-out pad: its tree terminal is the inner-layer breakout
            bx, by, blayer = esc[pad.pin_id]
            li = ws.layers.index(blayer)
            ix, iy = ws.to_cell(bx, by)
            target[blayer][iy, ix] = True
            target_centers.append((li, iy, ix))
            return
        cx, cy = ws.to_cell(pad.x, pad.y)
        for li, layer in enumerate(ws.layers):
            if layer not in pad.layers():
                continue
            geom = pad.geometry_on(layer)
            if geom is not None:
                iys, ixs = ws._cells_in_geom(geom, grow=0)
                target[layer][iys, ixs] = True
            target[layer][cy, cx] = True
            target_centers.append((li, cy, cx))

    # ---------------- single connection ---------------------------------
    OPT_MARGIN = 0.3  # optimistic A* margin (step units); exact check is truth
    LAYER_PENALTY = 8.0  # planar mode: per-cell cost of an off-assigned-layer
    NECK_RADIUS = 35.0  # mil around own pads where neck-down applies
    WIDTH_STEP = 3.93701  # 0.1 mm: width reductions happen in these steps

    def _best_width(self, net, layer, coords, floor_width: float) -> float:
        """Widest width (full down to floor, 0.1 mm steps) that fits exactly.
        Power traces keep as much copper as the geometry allows."""
        w = net.width
        while w > floor_width + 1e-6:
            if self.ws.exact_trace_ok(net.name, layer, coords, w, self._clr_for(net, w)):
                return w
            w -= self.WIDTH_STEP
        return floor_width

    TAPER_PIECE = 8.0  # mil: piece length for the graded width taper

    def _taper_runs(self, net, layer, pts, floor):
        """Split a narrow polyline into short pieces, each at the widest
        0.1 mm-stepped width that fits, ramping at most one step per piece.
        The trace is only as thin — and only for as long — as the pinch
        actually requires; everywhere else it recovers full width."""
        dense = [pts[0]]
        for p, q in zip(pts, pts[1:]):
            d = math.hypot(q[0] - p[0], q[1] - p[1])
            if d < 1e-6:
                continue
            n = max(1, int(math.ceil(d / self.TAPER_PIECE)))
            for t in range(1, n + 1):
                dense.append(
                    (p[0] + (q[0] - p[0]) * t / n, p[1] + (q[1] - p[1]) * t / n)
                )
        if len(dense) < 2:
            return [(list(pts), floor)]
        seg_w = [
            self._best_width(net, layer, dense[i : i + 2], floor)
            for i in range(len(dense) - 1)
        ]
        # ramp: width changes by at most one 0.1 mm step per piece in both
        # directions, so the neck widens gradually instead of jumping
        for i in range(1, len(seg_w)):
            seg_w[i] = min(seg_w[i], seg_w[i - 1] + self.WIDTH_STEP)
        for i in range(len(seg_w) - 2, -1, -1):
            seg_w[i] = min(seg_w[i], seg_w[i + 1] + self.WIDTH_STEP)
        self._shift_boundaries(dense, seg_w)
        out = []
        j = 0
        while j < len(seg_w):
            k = j
            while k + 1 < len(seg_w) and abs(seg_w[k + 1] - seg_w[j]) < 1e-6:
                k += 1
            out.append((dense[j : k + 2], seg_w[j]))
            j = k + 1
        return out

    def _shift_boundaries(self, dense, seg_w):
        """Move width-change boundaries off turn vertices. Runs of different
        widths cannot be chain-merged for filleting, so their junction must
        sit on a straight point — the narrower width takes the turn piece
        (shrinking a verified piece is always clearance-safe)."""
        n = len(seg_w)
        changed = True
        while changed:
            changed = False
            for j in range(1, n):
                if abs(seg_w[j] - seg_w[j - 1]) < 1e-6:
                    continue
                ax, ay = dense[j - 1]
                vx, vy = dense[j]
                bx, by = dense[j + 1]
                d1x, d1y = vx - ax, vy - ay
                d2x, d2y = bx - vx, by - vy
                l1, l2 = math.hypot(d1x, d1y), math.hypot(d2x, d2y)
                if l1 < 1e-6 or l2 < 1e-6:
                    continue
                cosv = max(-1.0, min(1.0, (d1x * d2x + d1y * d2y) / (l1 * l2)))
                if math.degrees(math.acos(cosv)) < 3.0:
                    continue  # boundary is on a straight point, fine
                if seg_w[j] < seg_w[j - 1]:
                    seg_w[j - 1] = seg_w[j]  # narrow grows backward over turn
                else:
                    seg_w[j] = seg_w[j - 1]  # narrow grows forward over turn
                changed = True

    def neck_width(self, net: Net) -> float:
        if self.strict_width:
            return net.width
        rule = net.net_class.rules.get("neck_down_width") if net.net_class else None
        return float(rule[0]) if rule else self.board.default_width

    def neck_gap(self, net: Net) -> float:
        """Reduced clearance allowed where a trace NECKS DOWN near fine-pitch
        pads. A high-clearance rule (e.g. a 15.7mil power/HV net) physically
        cannot be honored between 0.65mm-pitch pins that are ~9mil apart — the
        pads themselves already sit far closer than the rule — so a necked
        trace at the pins uses the class neck_down_gap (or the board default),
        and returns to FULL clearance the moment it widens out in the open."""
        if self.strict_width:
            return net.clearance
        rule = net.net_class.rules.get("neck_down_gap") if net.net_class else None
        if rule:
            return float(rule[0])
        return min(net.clearance, self.board.default_clearance)

    def _clr_for(self, net: Net, width: float) -> float:
        """Full clearance for full-width (open-board) copper; the reduced neck
        gap for narrowed near-pad copper. Keyed on width so the open board
        keeps its full clearance and only the pinch at the pins is relaxed."""
        if width < net.width - 1e-6:
            return self.neck_gap(net)
        return net.clearance

    def pad_entry_width(self, pad) -> float:
        """Copper width (narrow dimension) of a pad."""
        g = pad.geometry_on(next(iter(pad.layers())))
        if g is None:
            return 0.0
        b = g.bounds
        return min(b[2] - b[0], b[3] - b[1])

    def _floor_for(self, net: Net, pad) -> float:
        """Narrowest width any part of a connection from `pad` may use.
        A trace is never thinner than the pad it connects to: necking
        stops at the pad's own copper width."""
        return min(
            net.width, max(self.neck_width(net), self.pad_entry_width(pad))
        )

    def _neck_zone(self, net: Net, window):
        """Window cells within NECK_RADIUS of an own pad whose copper is
        narrower than the trace — where the trace may neck down (the DSN's
        pin_width_taper rule)."""
        ws = self.ws
        x0, y0, x1, y1 = window
        zone = np.zeros((y1 - y0 + 1, x1 - x0 + 1), dtype=bool)
        r = int(np.ceil(self.NECK_RADIUS / ws.step))
        for p in self.board.pads_of_net(net):
            g = p.geometry_on(next(iter(p.layers())))
            if g is None:
                continue
            b = g.bounds
            if min(b[2] - b[0], b[3] - b[1]) >= net.width:
                continue  # pad is wide enough, no neck needed there
            cx, cy = ws.to_cell(p.x, p.y)
            if not (x0 - r <= cx <= x1 + r and y0 - r <= cy <= y1 + r):
                continue
            ix0, ix1 = max(x0, cx - r), min(x1, cx + r)
            iy0, iy1 = max(y0, cy - r), min(y1, cy + r)
            if ix1 >= ix0 and iy1 >= iy0:
                zone[iy0 - y0 : iy1 - y0 + 1, ix0 - x0 : ix1 - x0 + 1] = True
        return zone

    def _in_neck(self, net: Net, x, y) -> bool:
        r = self.NECK_RADIUS
        for p in self.board.pads_of_net(net):
            if abs(p.x - x) <= r and abs(p.y - y) <= r:
                g = p.geometry_on(next(iter(p.layers())))
                if g is not None:
                    b = g.bounds
                    if min(b[2] - b[0], b[3] - b[1]) < net.width:
                        return True
        return False

    def _snap_end(self, geo, snap_line):
        """Project the final vertex onto snap_line (e.g. a coupled trace's
        exact centerline) so the junction lands on-track."""
        from shapely.geometry import Point

        runs, vias = geo
        if runs:
            layer, coords, width = runs[-1]
            if len(coords) >= 2:
                end = Point(coords[-1])
                snapped = snap_line.interpolate(snap_line.project(end))
                runs[-1] = (layer, list(coords[:-1]) + [(snapped.x, snapped.y)], width)
        return runs, vias

    def _route_to_tree(
        self, net: Net, pad, near_pt, target, target_centers, dist, own,
        windows=(150.0, 500.0, 1e9), via_dia: float | None = None,
        neck: bool = False, snap_line=None, bounds=None,
        floor_w: float | None = None,
    ):
        ws = self.ws
        step = ws.step
        if via_dia is None:
            via_dia = self.via_for(net)[1]
        nwidth = floor_w if floor_w is not None else self.neck_width(net)

        ax, ay = ws.to_cell(pad.x, pad.y)
        bx, by = ws.to_cell(*near_pt)

        for margin_mil in windows:
            m = int(margin_mil / step)
            x0 = max(0, min(ax, bx) - m)
            x1 = min(ws.nx - 1, max(ax, bx) + m)
            y0 = max(0, min(ay, by) - m)
            y1 = min(ws.ny - 1, max(ay, by) + m)
            window = (x0, y0, x1, y1)
            neck_zone = self._neck_zone(net, window) if neck else None
            neck_own = ws.own_exempt_mask(net.name, nwidth, bounds) if neck else None
            # optimistic pass first (exact validation is the judge),
            # pessimistic pass as fallback (forces a detour)
            for edt_margin in (self.OPT_MARGIN, self.edt_margin):
                req = net.width / 2 + net.clearance + edt_margin * step
                req_neck = nwidth / 2 + self.neck_gap(net) + edt_margin * step
                via_req = via_dia / 2 + net.clearance + edt_margin * step
                res = self._astar(
                    net, pad, target, target_centers, dist, own, req, via_req,
                    window, via_dia,
                    neck_zone=neck_zone, req_neck=req_neck, neck_own=neck_own,
                )
                if res is None:
                    continue
                for exact in (False, True):
                    geo = self._path_to_geometry(
                        net, res, pad, dist, own, req, exact=exact,
                        neck=neck, req_neck=req_neck, neck_own=neck_own,
                        floor_w=nwidth,
                    )
                    if geo is not None and snap_line is not None:
                        geo = self._snap_end(geo, snap_line)
                    if geo is not None and self._validate_exact(net, geo, via_dia):
                        return geo
            if x0 == 0 and y0 == 0 and x1 == ws.nx - 1 and y1 == ws.ny - 1:
                break
        return None

    def _validate_exact(self, net: Net, geo, via_dia: float | None = None) -> bool:
        if via_dia is None:
            via_dia = self.via_for(net)[1]
        runs, vias = geo
        for layer, coords, width in runs:
            if len(coords) >= 2 and not self.ws.exact_trace_ok(
                net.name, layer, coords, width, self._clr_for(net, width)
            ):
                return False
        for x, y in vias:
            if not self.ws.exact_via_ok(net.name, x, y, via_dia, net.clearance):
                return False
        return True

    def _pad_cells(self, pad, window):
        """(cells, centers): window-local (layer, iy, ix) source cells.

        If the pad has been FANNED OUT (escaped to an inner-layer breakout via
        a committed pad->via escape), routing starts from the breakout on the
        inner layer instead of the trapped pad — the escape is fixed copper."""
        ws = self.ws
        x0, y0, x1, y1 = window
        esc = getattr(self, "_escape", None)
        if esc and pad.pin_id in esc:
            bx, by, blayer = esc[pad.pin_id]
            li = ws.layers.index(blayer)
            ix, iy = ws.to_cell(bx, by)
            if x0 <= ix <= x1 and y0 <= iy <= y1:
                c = [(li, iy - y0, ix - x0)]
                return c, list(c)
            return [], []
        cells = []
        for li, layer in enumerate(ws.layers):
            if layer not in pad.layers():
                continue
            geom = pad.geometry_on(layer)
            if geom is None:
                continue
            iys, ixs = ws._cells_in_geom(geom)
            sel = (ixs >= x0) & (ixs <= x1) & (iys >= y0) & (iys <= y1)
            for iy, ix in zip(iys[sel], ixs[sel]):
                cells.append((li, iy - y0, ix - x0))
        centers = []
        cx, cy = ws.to_cell(pad.x, pad.y)
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            for li, layer in enumerate(ws.layers):
                if layer in pad.layers():
                    centers.append((li, cy - y0, cx - x0))
        return cells, centers

    def _astar(
        self, net, pad, target, target_centers, dist, own, req, via_req, window,
        via_dia: float | None = None,
        neck_zone=None, req_neck: float = 0.0, neck_own=None,
    ):
        ws = self.ws
        step = ws.step
        if via_dia is None:
            via_dia = self.via_diameter
        x0, y0, x1, y1 = window
        wx, wy = x1 - x0 + 1, y1 - y0 + 1
        nl = len(ws.layers)

        # planar mode: a net PREFERS its assigned routing layer(s) — off-
        # layer cells cost a per-cell penalty (soft), so a net stays on its
        # layer where it can and detours (via) only when it must
        pref = getattr(net, "pref_layers", None)
        trav = np.empty((nl, wy, wx), dtype=bool)
        goal_mask = np.empty((nl, wy, wx), dtype=bool)
        for li, layer in enumerate(ws.layers):
            d = dist[layer][y0 : y1 + 1, x0 : x1 + 1]
            trav[li] = (d >= req) | own[layer][y0 : y1 + 1, x0 : x1 + 1]
            if neck_zone is not None:
                narrow_ok = (d >= req_neck) | neck_own[layer][y0 : y1 + 1, x0 : x1 + 1]
                trav[li] |= neck_zone & narrow_ok
            goal_mask[li] = target[layer][y0 : y1 + 1, x0 : x1 + 1]
        via_ok = ws.pad_distance()[y0 : y1 + 1, x0 : x1 + 1] >= via_dia / 2 + 1.0
        for layer in ws.layers:
            via_ok &= dist[layer][y0 : y1 + 1, x0 : x1 + 1] >= via_req

        starts, start_centers = self._pad_cells(pad, window)
        starts += start_centers
        for li, iy, ix in start_centers:
            trav[li, iy, ix] = True
        # goal cells need no force-trav: the kernel accepts goals on arrival

        # don't start on the target (already connected / overlapping pads)
        for li, iy, ix in starts:
            if goal_mask[li, iy, ix]:
                return "already"

        if not starts or not goal_mask.any():
            return None

        gys, gxs = np.nonzero(goal_mask.any(axis=0))
        g_iy0, g_iy1 = gys.min(), gys.max()
        g_ix0, g_ix1 = gxs.min(), gxs.max()

        from .astar_kernel import astar as astar_jit

        # Soft cost shaping is limited to NECK ZONES (centering the path so
        # the width taper stays fat). Search-wide penalties measurably hurt
        # completion — style rules are applied as post-passes instead.
        cong = np.zeros((nl, wy, wx), dtype=np.float32)
        if neck_zone is not None:
            for li, layer in enumerate(ws.layers):
                d = dist[layer][y0 : y1 + 1, x0 : x1 + 1]
                cong[li] = np.where(
                    neck_zone & (d < req), (req - d) * 0.6, 0.0
                ).astype(np.float32)
        if self._corridor_bias is not None:
            # pathfinder mode: attract the search to the negotiated corridor
            for li, layer in enumerate(ws.layers):
                b = self._corridor_bias.get(layer)
                if b is not None:
                    cong[li] += b[y0 : y1 + 1, x0 : x1 + 1]
        if pref is not None:
            # planar mode: per-cell penalty for straying off the net's
            # assigned layer(s) — keeps it planar but allows via detours
            for li, layer in enumerate(ws.layers):
                if layer not in pref:
                    cong[li] += self.LAYER_PENALTY
        lbase = getattr(self, "_layer_base", None)
        if lbase:
            # per-layer base cost — used to make overflow/inner layers a last
            # resort so Manhattan routes on the primary H/V pair first
            for li, layer in enumerate(ws.layers):
                c = lbase.get(layer)
                if c:
                    cong[li] += c

        layer_stride = wy * wx
        start_states = np.array(
            sorted({li * layer_stride + iy * wx + ix for li, iy, ix in starts}),
            dtype=np.int64,
        )
        grain_map = getattr(self, "_grain", None)
        if grain_map is not None:
            # Manhattan mode: per-layer grain (H/V), orthogonal-only search
            from .astar_kernel import astar_grain
            grain = np.array(
                [grain_map.get(l, -1) for l in ws.layers], dtype=np.int8
            )
            found, parent = astar_grain(
                trav.reshape(-1), goal_mask.reshape(-1), via_ok.reshape(-1),
                cong.reshape(-1), start_states, nl, wy, wx, step, self.via_cost,
                g_ix0, g_ix1, g_iy0, g_iy1,
                grain, getattr(self, "_grain_pen", 3.0),
            )
        else:
            found, parent = astar_jit(
                trav.reshape(-1),
                goal_mask.reshape(-1),
                via_ok.reshape(-1),
                cong.reshape(-1),
                start_states,
                nl, wy, wx,
                step, self.via_cost,
                g_ix0, g_ix1, g_iy0, g_iy1,
            )
        if found < 0:
            return None
        path = []
        s = found
        while s >= 0:
            li, rem = divmod(s, layer_stride)
            iy, ix = divmod(rem, wx)
            path.append((li, iy + y0, ix + x0))
            s = parent[s]
        path.reverse()
        return path

    # ---------------- beautify post-pass ---------------------------------
    def prune_open_stubs(self) -> int:
        """Remove trace stubs whose free end connects to nothing (rip-up
        and failed edges can leave them). A trace is pruned only when no
        other copper touches it away from its connected end, so pruning
        never disconnects real copper; runs iteratively from the tips."""
        from shapely.geometry import LineString, Point

        removed_total = 0
        while True:
            by_net: dict[str, list] = {}
            for t in self.result.traces:
                by_net.setdefault(t.net, []).append(t)
            vias_by_net: dict[str, list] = {}
            for v in self.result.vias:
                vias_by_net.setdefault(v.net, []).append(v)
            pads_by_net: dict[str, list] = {}
            for p in self.board.pads.values():
                if p.net:
                    pads_by_net.setdefault(p.net, []).append(p)

            to_remove = []
            for net, traces in by_net.items():
                pieces = []  # (layer or '*', geom, trace index or -1)
                for i, t in enumerate(traces):
                    pieces.append(
                        (t.layer, LineString(t.coords).buffer(t.width / 2), i)
                    )
                for v in vias_by_net.get(net, []):
                    pieces.append(("*", Point(v.x, v.y).buffer(v.diameter / 2), -1))
                for p in pads_by_net.get(net, []):
                    g = p.geometry_on(next(iter(p.layers())))
                    if g is None:
                        continue
                    pieces.append(
                        ("*" if p.padstack.is_through() else next(iter(p.layers())), g, -1)
                    )
                for i, t in enumerate(traces):
                    if len(t.coords) < 2:
                        continue
                    for free, keep in (
                        (t.coords[0], t.coords[-1]),
                        (t.coords[-1], t.coords[0]),
                    ):
                        pt = Point(free)
                        others = [
                            (g, j) for ly, g, j in pieces
                            if j != i and (ly == "*" or ly == t.layer)
                        ]
                        if any(g.distance(pt) < t.width / 2 - 1e-3 for g, _ in others):
                            continue  # this end is connected
                        # free end dangles: prune only if nothing attaches
                        # to the body away from the kept end
                        body = LineString(t.coords)
                        keep_pt = Point(keep)
                        safe = all(
                            g.distance(body) >= t.width / 2 - 1e-3
                            or g.distance(keep_pt) < t.width
                            for g, _ in others
                        )
                        if safe:
                            to_remove.append(t)
                        break
            if not to_remove:
                break
            drop = set(map(id, to_remove))
            self.result.traces = [
                t for t in self.result.traces if id(t) not in drop
            ]
            removed_total += len(to_remove)
        return removed_total

    def beautify_exits(self):
        """After routing completes, graft straight pad-exit stubs (~one pad
        length before the first turn) onto finished traces where room allows.
        Each graft is exact-validated; the stub corridor is registered so
        later passes (fillet) respect it."""
        from shapely.geometry import LineString, Point

        pads_by_center = {}
        for p in self.board.pads.values():
            pads_by_center[(round(p.x, 2), round(p.y, 2), p.net)] = p
        # same-net attachment points that a graft must never orphan:
        # other traces' endpoints and via centers
        junctions: dict[str, list] = {}
        for tr in self.result.traces:
            if len(tr.coords) >= 2:
                junctions.setdefault(tr.net, []).append(
                    (tr.layer, tr.coords[0], tr.width)
                )
                junctions.setdefault(tr.net, []).append(
                    (tr.layer, tr.coords[-1], tr.width)
                )
        for v in self.result.vias:
            junctions.setdefault(v.net, []).append(("*", (v.x, v.y), v.diameter))
        grafted = 0
        for t in self.result.traces:
            if t.no_fillet or len(t.coords) < 3:
                continue
            net = self.board.nets.get(t.net)
            if net is None:
                continue
            for tail in (False, True):
                pts = list(reversed(t.coords)) if tail else list(t.coords)
                key = (round(pts[0][0], 2), round(pts[0][1], 2), t.net)
                pad = pads_by_center.get(key)
                if pad is None:
                    continue
                new_pts, k = self._exit_stub(net, pad, pts, t.layer, t.width)
                if not k:
                    continue
                # anything attached to the replaced section must still
                # touch the new path, or the graft would open the net
                old_sec = LineString(pts[: k + 1])
                new_sec = LineString(new_pts[:3])
                safe = True
                for ly, (jx, jy), jw in junctions.get(t.net, ()):
                    if ly != "*" and ly != t.layer:
                        continue
                    jp = Point(jx, jy)
                    r = t.width / 2 + jw / 2
                    if (
                        jp.distance(old_sec) < r - 1e-3
                        and jp.distance(new_sec) > r - 1e-3
                    ):
                        safe = False
                        break
                if not safe:
                    continue
                t.coords = list(reversed(new_pts)) if tail else new_pts
                self.ws.add_trace(t.net, t.layer, new_pts[:3], t.width)
                grafted += 1
        return grafted

    def _fit_width(self, net, layer, coords, lo: float, hi: float) -> float:
        """Widest width in [lo, hi] (0.1 mm steps down from hi) that keeps
        exact clearance; lo is assumed to fit (the trace exists at lo)."""
        w = hi
        while w > lo + 1e-6:
            if self.ws.exact_trace_ok(net.name, layer, coords, w, self._clr_for(net, w)):
                return w
            w -= self.WIDTH_STEP
        return lo

    def fatten_pad_entries(self) -> int:
        """A trace is never thinner than the pad it connects to: widen each
        pad entry up to the pad's copper width (as far as exact clearance
        allows), stepping back down to the class width 0.1 mm per piece.
        Runs after routing; every widened piece is exact-validated and
        registered so DRC and later passes see the real copper."""
        ws = self.ws
        pads_by_net: dict[str, list] = {}
        for p in self.board.pads.values():
            if p.net:
                pads_by_net.setdefault(p.net, []).append(p)
        vias_by_net: dict[str, list] = {}
        for v in self.result.vias:
            vias_by_net.setdefault(v.net, []).append(v)

        out = []
        fattened = 0
        for t in self.result.traces:
            net = self.board.nets.get(t.net)
            if (
                net is None
                or t.net in self.result.diffpair_nets  # keep pair geometry
                or len(t.coords) < 2
                or t.net not in pads_by_net
            ):
                out.append(t)
                continue

            # pad (if any) at each end whose copper is wider than the trace;
            # hold = half the pad length, so the entry keeps full pad width
            # from the pad CENTER to its edge before tapering (structured
            # teardrop emanating from the center)
            targets = []
            for end in (t.coords[0], t.coords[-1]):
                hit, hold = 0.0, 0.0
                for p in pads_by_net[t.net]:
                    if abs(p.x - end[0]) > 40 or abs(p.y - end[1]) > 40:
                        continue
                    g = p.geometry_on(t.layer)
                    if g is None:
                        continue
                    b = g.bounds
                    if not (b[0] - 1 < end[0] < b[2] + 1 and b[1] - 1 < end[1] < b[3] + 1):
                        continue
                    if min(b[2] - b[0], b[3] - b[1]) > hit:
                        hit = min(b[2] - b[0], b[3] - b[1])
                        hold = max(b[2] - b[0], b[3] - b[1]) / 2
                # vias get teardrops too (a via is a pad on every layer)
                for v in vias_by_net.get(t.net, ()):
                    if (
                        v.diameter > hit
                        and abs(v.x - end[0]) < v.diameter
                        and abs(v.y - end[1]) < v.diameter
                        and math.hypot(v.x - end[0], v.y - end[1])
                        < v.diameter / 2 + 1.0
                    ):
                        hit = v.diameter
                        hold = v.diameter / 2
                targets.append((hit, hold))
            if max(tw for tw, _ in targets) <= t.width + 1e-6:
                out.append(t)
                continue

            # densify, then per piece: want = ramp from each pad end down
            # to the trace width, actual = widest that fits exactly
            dense = [t.coords[0]]
            for p, q in zip(t.coords, t.coords[1:]):
                d = math.hypot(q[0] - p[0], q[1] - p[1])
                if d < 1e-6:
                    continue
                n = max(1, int(math.ceil(d / self.TAPER_PIECE)))
                for s in range(1, n + 1):
                    dense.append(
                        (p[0] + (q[0] - p[0]) * s / n, p[1] + (q[1] - p[1]) * s / n)
                    )
            if len(dense) < 2:
                out.append(t)
                continue
            nseg = len(dense) - 1
            # arclength midpoint of each piece, measured from each end
            mids = []
            s = 0.0
            for i in range(nseg):
                d = math.hypot(
                    dense[i + 1][0] - dense[i][0], dense[i + 1][1] - dense[i][1]
                )
                mids.append(s + d / 2)
                s += d
            total = s
            (t0w, h0), (t1w, h1) = targets
            seg_w = []
            for i in range(nseg):
                want = t.width
                if t0w > 0:
                    steps = max(0.0, math.ceil((mids[i] - h0) / self.TAPER_PIECE))
                    want = max(want, t0w - steps * self.WIDTH_STEP)
                if t1w > 0:
                    steps = max(
                        0.0, math.ceil((total - mids[i] - h1) / self.TAPER_PIECE)
                    )
                    want = max(want, t1w - steps * self.WIDTH_STEP)
                if want <= t.width + 1e-6:
                    seg_w.append(t.width)
                    continue
                seg_w.append(
                    self._fit_width(net, t.layer, dense[i : i + 2], t.width, want)
                )
            if max(seg_w) <= t.width + 1e-6:
                out.append(t)
                continue
            self._shift_boundaries(dense, seg_w)

            fattened += 1
            j = 0
            while j < nseg:
                k = j
                while k + 1 < nseg and abs(seg_w[k + 1] - seg_w[j]) < 1e-6:
                    k += 1
                piece = Trace(
                    t.net, t.layer, dense[j : k + 2], seg_w[j],
                    no_fillet=t.no_fillet,
                )
                out.append(piece)
                if seg_w[j] > t.width + 1e-6:
                    ws.add_trace(t.net, t.layer, piece.coords, seg_w[j])
                j = k + 1
        self.result.traces = out
        return fattened

    # ---------------- geometry post-processing --------------------------
    def _exit_stub(self, net, pad, pts, layer, width):
        """Leave the pad straight for ~one pad length before any direction
        change (when there is room). The stub aims at the connection target
        when that is clear, else falls back to the pad axis nearest that
        direction. Returns (pts, pinned)."""
        if len(pts) < 2:
            return pts, 0
        g = pad.geometry_on(layer)
        if g is None:
            return pts, 0
        b = g.bounds
        w, h = b[2] - b[0], b[3] - b[1]
        L = max(w, h)
        if L < 10.0:
            return pts, 0
        px, py = pts[0]
        # direction toward the far end of the route (the copper we join)
        tx, ty = pts[-1][0] - px, pts[-1][1] - py
        lt = math.hypot(tx, ty)
        if lt < 1e-6:
            return pts, 0
        cands = [(tx / lt, ty / lt)]
        if abs(w - h) < 1e-6:
            axes = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        elif w > h:
            axes = [(1, 0), (-1, 0)]
        else:
            axes = [(0, 1), (0, -1)]
        cands += sorted(axes, key=lambda a: -(a[0] * tx + a[1] * ty))
        for ax, ay in cands:
            for f in (1.0, 0.7, 0.5):
                tip = (px + ax * L * f, py + ay * L * f)
                if not self.ws.exact_trace_ok(
                    net.name, layer, [pts[0], tip], width, self._clr_for(net, width)
                ):
                    continue
                # rejoin the found path at the farthest exactly-visible vertex
                hi = len(pts) - 1
                for k in range(hi, 0, -1):
                    if hi - k > 15:
                        break
                    # no spikes: the bend at the stub tip must stay gentle
                    rx, ry = pts[k][0] - tip[0], pts[k][1] - tip[1]
                    lr = math.hypot(rx, ry)
                    if lr < 1e-6:
                        continue
                    if (ax * rx + ay * ry) / lr < 0.25:  # > ~75 deg turn
                        continue
                    if self.ws.exact_trace_ok(
                        net.name, layer, [tip, pts[k]], width, self._clr_for(net, width)
                    ):
                        return [pts[0], tip] + list(pts[k:]), k
        return pts, 0

    def _path_to_geometry(
        self, net, path, pad, dist, own, req, exact=False,
        neck=False, req_neck: float = 0.0, neck_own=None,
        floor_w: float | None = None,
    ):
        """Cell path -> per-layer polyline runs (string-pulled) + via points.
        Runs are (layer, coords, width); in neck mode a run is split into
        narrow sections near own pads and full-width sections elsewhere."""
        if path == "already":
            return [], []
        ws = self.ws
        nwidth = floor_w if floor_w is not None else self.neck_width(net)

        runs_cells = []
        cur_layer = path[0][0]
        cur = [path[0]]
        vias = []
        for node in path[1:]:
            if node[0] != cur_layer:
                runs_cells.append((cur_layer, cur))
                x, y = ws.to_world(node[2], node[1])
                vias.append((x, y))
                cur_layer = node[0]
                cur = [node]
            else:
                cur.append(node)
        runs_cells.append((cur_layer, cur))

        runs = []
        for k, (li, cells) in enumerate(runs_cells):
            layer = ws.layers[li]
            pts = [ws.to_world(ix, iy) for _, iy, ix in cells]
            pinned = 0
            if k == 0:
                pts[0] = (pad.x, pad.y)  # exact start at the new pad center
                # (pad-exit stubs are grafted in the beautify post-pass)

            if not neck:
                check = None
                if exact:
                    check = lambda p, q, _l=layer: ws.exact_trace_ok(
                        net.name, _l, [p, q], net.width, net.clearance
                    )
                if pinned:
                    tail = self._string_pull(
                        pts[1:], dist[layer], own[layer], req, check
                    )
                    pts = [pts[0]] + tail
                else:
                    pts = self._string_pull(pts, dist[layer], own[layer], req, check)
                runs.append((layer, pts, net.width))
                continue

            # neck mode: split into narrow (near own small pads) / full sections
            flags = [self._in_neck(net, x, y) for x, y in pts]
            sections = []
            s = 0
            for e in range(1, len(pts)):
                if flags[e] != flags[s]:
                    sections.append((flags[s], pts[s : e + 1], s == 0))
                    s = e
            sections.append((flags[s], pts[s:], s == 0))
            for narrow, sec, is_first in sections:
                if len(sec) < 2:
                    continue
                w = nwidth if narrow else net.width
                # pull narrow sections at FULL-width clearance: shortcuts must
                # not drag the centered path back against the walls (where no
                # width upgrade could ever fit)
                r = req
                own_l = (neck_own if narrow else own)[layer]
                check = None
                if exact:
                    check = lambda p, q, _l=layer, _w=w: ws.exact_trace_ok(
                        net.name, _l, [p, q], _w, self._clr_for(net, _w)
                    )
                if pinned and is_first and len(sec) > 2:
                    # keep the straight pad-exit stub intact
                    sec = [sec[0]] + self._string_pull(sec[1:], dist[layer], own_l, r, check)
                else:
                    sec = self._string_pull(sec, dist[layer], own_l, r, check)
                if not narrow:
                    runs.append((layer, sec, w))
                    continue
                # narrow section validated at neck width; _connect_pad
                # tapers it back up piecewise once the route is accepted
                runs.append((layer, sec, nwidth))
        return runs, vias

    def _clear_segment(self, p, q, d_layer, own_layer, req) -> bool:
        ws = self.ws
        return seg_clear(
            p[0], p[1], q[0], q[1], d_layer, own_layer, req,
            ws.x0, ws.y0, ws.step, ws.nx, ws.ny,
        )

    @staticmethod
    def _orthogonal_simplify(pts):
        """Collapse collinear points, keeping only the corners — pure H/V
        segments between turns (Manhattan mode)."""
        if len(pts) <= 2:
            return pts
        out = [pts[0]]
        for i in range(1, len(pts) - 1):
            ax, ay = pts[i - 1]
            bx, by = pts[i]
            cx, cy = pts[i + 1]
            # keep b only if the direction actually changes at it
            if (bx - ax) * (cy - by) - (by - ay) * (cx - bx) != 0:
                out.append(pts[i])
        out.append(pts[-1])
        return out

    def _string_pull(self, pts, d_layer, own_layer, req, exact_check=None):
        """Greedy line-of-sight shortcutting; optional exact-geometry check.
        The no-exact-check path (the hot one during search) runs entirely in
        a nogil numba kernel so parallel routing isn't serialized by the
        Python interpreter lock."""
        if len(pts) <= 2:
            return pts
        if getattr(self, "_orthogonal", False):
            # Manhattan mode: keep PURE orthogonal geometry — no diagonal
            # line-of-sight shortcuts. Just collapse collinear runs to their
            # corners; the fillet pass rounds those corners afterwards.
            return self._orthogonal_simplify(pts)
        ws = self.ws
        if exact_check is None:
            arr = np.asarray(pts, dtype=np.float64)
            keep = string_pull(
                np.ascontiguousarray(arr[:, 0]),
                np.ascontiguousarray(arr[:, 1]),
                d_layer, own_layer, float(req),
                ws.x0, ws.y0, ws.step, ws.nx, ws.ny,
            )
            return [pts[int(k)] for k in keep]
        out = [pts[0]]
        i = 0
        while i < len(pts) - 1:
            j = len(pts) - 1
            while j > i + 1:
                if self._clear_segment(pts[i], pts[j], d_layer, own_layer, req) and (
                    exact_check(pts[i], pts[j])
                ):
                    break
                j -= 1
            out.append(pts[j])
            i = j
        return out
