"""PathFinder negotiated-congestion routing (alternative method).

Classic FPGA-style negotiation (McMurchie/Ebeling, VPR flavour) adapted to
PCB routing:

  1. NEGOTIATE on a coarse grid: every net routes as if alone; cells shared
     by several nets charge a present-sharing penalty that grows each
     iteration, plus an accumulating history penalty. Nets gradually
     abandon contested corridors until no cell is over-subscribed.
  2. REALIZE on the fine grid with the standard exact-geometry pipeline
     (Guided-Chaos machinery), each net's search attracted to its
     negotiated corridor.
  3. REPAIR any residue with the proven ladder (rip-up, shake, endgame).

The default "chaos" method remains untouched; this is selected with
method="pathfinder".
"""

from __future__ import annotations

import math

import numpy as np

from .astar_kernel import astar
from .fields import distance_field


class Negotiator:
    """Coarse-grid congestion negotiation."""

    COARSE = 8.0  # mil
    MAX_ITERS = 24
    HIST_FAC = 0.35    # history cost weight (grows in effect via accumulation)
    PRES_BASE = 0.6    # present-sharing weight, multiplied by iteration

    def __init__(self, board, ws, router, progress=None):
        self.board = board
        self.ws = ws
        self.router = router
        self.progress = progress
        self.step = self.COARSE
        b = board.outline.bounds
        self.x0, self.y0 = b[0] - 20, b[1] - 20
        self.nx = int(np.ceil((b[2] - b[0] + 40) / self.step)) + 1
        self.ny = int(np.ceil((b[3] - b[1] + 40) / self.step)) + 1
        self.layers = list(board.layers)
        self.nl = len(self.layers)
        shape = (self.nl, self.ny, self.nx)
        self.history = np.zeros(shape, dtype=np.float32)
        self.usage = np.zeros(shape, dtype=np.int16)
        self.net_cells: dict[str, list] = {}   # net -> [(li, iy, ix), ...] flat arrays
        self.net_paths: dict[str, list] = {}   # net -> list of cell paths (edges)
        self._pad_dist_cache: dict[str, np.ndarray] = {}
        self._pad_masks()

    # ---- static geometry -------------------------------------------------
    def to_cell(self, x, y):
        ix = int(round((x - self.x0) / self.step))
        iy = int(round((y - self.y0) / self.step))
        return (min(max(ix, 0), self.nx - 1), min(max(iy, 0), self.ny - 1))

    def _pad_masks(self):
        """Per-layer mask of pad copper cells and the owning net id."""
        from shapely import contains_xy

        self.pad_owner = np.full((self.nl, self.ny, self.nx), -1, dtype=np.int32)
        self.net_index = {name: i for i, name in enumerate(self.board.nets)}
        xs = self.x0 + np.arange(self.nx) * self.step
        ys = self.y0 + np.arange(self.ny) * self.step
        gx, gy = np.meshgrid(xs, ys)
        # outside board = blocked marker -2
        inside = contains_xy(self.board.outline, gx.ravel(), gy.ravel()).reshape(gy.shape)
        for li in range(self.nl):
            self.pad_owner[li][~inside] = -2
        for pad in self.board.pads.values():
            nid = self.net_index.get(pad.net, -2)  # no-net pads block everyone
            for li, layer in enumerate(self.layers):
                if layer not in pad.layers():
                    continue
                g = pad.geometry_on(layer)
                if g is None:
                    continue
                b = g.buffer(self.step * 0.5).bounds
                ix0 = max(0, int((b[0] - self.x0) / self.step))
                ix1 = min(self.nx - 1, int(np.ceil((b[2] - self.x0) / self.step)))
                iy0 = max(0, int((b[1] - self.y0) / self.step))
                iy1 = min(self.ny - 1, int(np.ceil((b[3] - self.y0) / self.step)))
                if ix1 < ix0 or iy1 < iy0:
                    continue
                sub_x, sub_y = np.meshgrid(xs[ix0:ix1 + 1], ys[iy0:iy1 + 1])
                m = contains_xy(g.buffer(self.step * 0.5), sub_x.ravel(),
                                sub_y.ravel()).reshape(sub_y.shape)
                blk = self.pad_owner[li, iy0:iy1 + 1, ix0:ix1 + 1]
                blk[m] = np.where(blk[m] == -2, -2, nid)

    def _foreign_pad_dist(self, net_name: str) -> np.ndarray:
        """Distance (mil) to the nearest pad NOT of this net, per layer.
        Cached per net (pads are static during negotiation)."""
        d = self._pad_dist_cache.get(net_name)
        if d is not None:
            return d
        nid = self.net_index.get(net_name, -3)
        out = np.empty((self.nl, self.ny, self.nx), dtype=np.float16)
        for li in range(self.nl):
            mask = (self.pad_owner[li] != -1) & (self.pad_owner[li] != nid)
            out[li] = distance_field(mask, self.step).astype(np.float16)
        self._pad_dist_cache[net_name] = out
        return out

    # ---- negotiation ------------------------------------------------------
    def _route_net_edges(self, net, pres_fac: float):
        """Route all Prim edges of a net on the coarse grid; returns cell
        paths and the set of cells used."""
        ws_step = self.step
        pads = self.board.pads_of_net(net)
        if len(pads) < 2:
            return [], []
        req = net.width / 2 + net.clearance
        dist = self._foreign_pad_dist(net.name).astype(np.float32)
        nid = self.net_index[net.name]
        trav = np.zeros((self.nl, self.ny, self.nx), dtype=bool)
        for li in range(self.nl):
            trav[li] = (dist[li] >= req) | (self.pad_owner[li] == nid)
            trav[li] &= self.pad_owner[li] != -2

        # congestion cost: history + present usage of other nets
        cong = self.HIST_FAC * self.history * ws_step
        usage = self.usage.astype(np.float32)
        cong += pres_fac * np.maximum(usage, 0.0) * ws_step
        cong = cong.astype(np.float32)

        pts = np.array([(p.x, p.y) for p in pads])
        dmat = np.hypot(pts[:, 0, None] - pts[None, :, 0],
                        pts[:, 1, None] - pts[None, :, 1])
        start = int(np.argmin(dmat.sum(axis=1)))
        target = np.zeros((self.nl, self.ny, self.nx), dtype=bool)
        centers = []

        def add_pad(p):
            ix, iy = self.to_cell(p.x, p.y)
            for li, layer in enumerate(self.layers):
                if layer in p.layers():
                    target[li, iy, ix] = True
                    centers.append((li, iy, ix))

        add_pad(pads[start])
        connected = [start]
        remaining = set(range(len(pads))) - {start}
        paths = []
        cells: list = []
        via_ok = np.ones((self.ny, self.nx), dtype=bool)
        for li in range(self.nl):
            via_ok &= self.pad_owner[li] != -2
        via_ok &= (dist.min(axis=0) >= 10.0)

        while remaining:
            j = min(remaining, key=lambda k: min(dmat[k][c] for c in connected))
            remaining.discard(j)
            connected.append(j)
            p = pads[j]
            sx, sy = self.to_cell(p.x, p.y)
            starts = []
            for li, layer in enumerate(self.layers):
                if layer in p.layers():
                    starts.append((li, sy, sx))
            if not starts:
                continue
            g_ix0 = min(c[2] for c in centers)
            g_ix1 = max(c[2] for c in centers)
            g_iy0 = min(c[1] for c in centers)
            g_iy1 = max(c[1] for c in centers)
            stride = self.ny * self.nx
            start_states = np.array(
                sorted({li * stride + iy * self.nx + ix for li, iy, ix in starts}),
                dtype=np.int64,
            )
            found, parent = astar(
                trav.reshape(-1), target.reshape(-1), via_ok.reshape(-1),
                cong.reshape(-1), start_states,
                self.nl, self.ny, self.nx, self.step, 60.0,
                g_ix0, g_ix1, g_iy0, g_iy1,
            )
            if found < 0:
                continue  # unreachable on coarse grid; realization will try
            path = []
            s = found
            while s >= 0:
                li, rem = divmod(s, stride)
                iy, ix = divmod(rem, self.nx)
                path.append((int(li), int(iy), int(ix)))
                s = parent[s]
            path.reverse()
            paths.append(path)
            for li, iy, ix in path:
                target[li, iy, ix] = True
                cells.append((li, iy, ix))
            centers.append(path[0])
            add_pad(p)
        return paths, cells

    def negotiate(self, nets) -> dict[str, list]:
        """Iterate until no coarse cell is claimed by more than one net.
        Returns net -> list of cell paths (the corridors)."""
        say = self.progress or (lambda s: None)
        order = list(nets)
        for it in range(self.MAX_ITERS):
            pres = self.PRES_BASE * (1.6 ** it) if it else 0.0
            for net in order:
                # remove own usage, re-route with penalties
                for li, iy, ix in self.net_cells.get(net.name, ()):
                    self.usage[li, iy, ix] -= 1
                paths, cells = self._route_net_edges(net, pres)
                self.net_paths[net.name] = paths
                self.net_cells[net.name] = cells
                for li, iy, ix in cells:
                    self.usage[li, iy, ix] += 1
            over = int((self.usage > 1).sum())
            say(f"  pathfinder iter {it + 1}: {over} over-subscribed cells")
            if over == 0:
                break
            self.history += (self.usage > 1).astype(np.float32)
        return self.net_paths


def route_all_pathfinder(router, progress=None):
    """Full pathfinder pipeline: pairs first (rigid), negotiate the rest,
    realize with exact geometry biased to corridors, repair residue."""
    from .diffpair import find_diff_pairs, route_diff_pair

    board, ws = router.board, router.ws
    say = (lambda s: progress(0, 0, s, router.result)) if progress else (lambda s: None)

    for net_p, net_n, gap in find_diff_pairs(board):
        if route_diff_pair(router, net_p, net_n, gap):
            router.result.diffpair_nets |= {net_p.name, net_n.name}

    nets = [n for n in router.net_order()
            if n.name not in router.result.diffpair_nets]
    say(f"pathfinder: negotiating {len(nets)} nets on coarse grid")
    neg = Negotiator(board, ws, router, progress=say)
    corridors = neg.negotiate(nets)

    say("pathfinder: realizing negotiated corridors (exact geometry)")
    for i, net in enumerate(nets):
        bias = _corridor_bias(neg, corridors.get(net.name, []), ws)
        router.set_corridor_bias(bias)
        try:
            router.route_net(net)
        finally:
            router.set_corridor_bias(None)
        if progress and (i + 1) % 20 == 0:
            progress(i + 1, len(nets), net.name, router.result)

    # residue -> the proven repair ladder
    if router.result.failed:
        say(f"pathfinder: {len(router.result.failed)} residue -> repair ladder")
        router._rip_and_retry(progress)
        router._shake(progress)
        router._endgame(progress)
        if router.result.failed:
            router._shake(progress)
    return router.result


def _corridor_bias(neg: Negotiator, paths, ws, weight: float = 0.02,
                   cap: float = 250.0):
    """Fine-grid penalty per layer: 0 inside the negotiated corridor and
    growing with distance from it (capped), so the exact search follows
    the negotiated plan but may deviate locally for exact clearance."""
    if not paths:
        return None
    corridor = np.zeros((neg.nl, neg.ny, neg.nx), dtype=bool)
    for path in paths:
        for li, iy, ix in path:
            corridor[li, iy, ix] = True
    # fine-cell -> coarse-cell index maps (grids share the board frame but
    # differ in step and margins)
    fx = np.clip(np.round(
        (ws.x0 + np.arange(ws.nx) * ws.step - neg.x0) / neg.step
    ).astype(np.int32), 0, neg.nx - 1)
    fy = np.clip(np.round(
        (ws.y0 + np.arange(ws.ny) * ws.step - neg.y0) / neg.step
    ).astype(np.int32), 0, neg.ny - 1)
    bias = {}
    for li, layer in enumerate(ws.layers):
        d = distance_field(corridor[li], neg.step)  # 0 on corridor cells
        fine = d[fy[:, None], fx[None, :]]
        bias[layer] = (np.minimum(fine, cap) * weight * ws.step).astype(
            np.float32
        )
    return bias
