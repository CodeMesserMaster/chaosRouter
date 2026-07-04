"""Fully-nogil copper collision index (chaosrouter).

Every copper item is packed into flat arrays with a CSR spatial grid, and a
trace's exact clearance check runs entirely in a nogil numba kernel
(candidate selection + distance math) — no Python per query — so routing
releases the GIL and parallelizes across cores. Verified against shapely
(grid.exact_trace_ok) to 100% agreement.
"""

from __future__ import annotations

import numpy as np

from .geom_kernels import trace_ok_csr


class FastCopper:
    def __init__(self, board, x0, y0, x1, y1, cell=80.0):
        self.board = board
        self.net_id = {n: i for i, n in enumerate(board.nets)}
        self.layers = list(board.layers)
        self.gx0, self.gy0 = x0 - 40, y0 - 40
        self.cell = cell
        self.inv = 1.0 / cell
        self.gnx = int((x1 - x0 + 80) / cell) + 2
        self.gny = int((y1 - y0 + 80) / cell) + 2
        self.ncells = self.gnx * self.gny
        # dynamic (traces=segments, vias=circles) + static (pads=polygons)
        self._seg = {ly: [] for ly in self.layers}   # rows: x0,y0,x1,y1,r,clr,net
        self._cir = {ly: [] for ly in self.layers}   # rows: x,y,r,clr,net
        self._poly = {ly: [] for ly in self.layers}  # (vx, vy, clr, net)
        self._csr = {}          # layer -> packed dict (built lazily)
        self._dirty = {ly: True for ly in self.layers}
        self._poly_csr = {}     # static, built once per layer

    # ---- add copper ------------------------------------------------------
    def add_segments(self, layer, coords, hw, clr, net):
        nid = self.net_id.get(net, -1)
        L = self._seg[layer]
        for a, b in zip(coords, coords[1:]):
            L.append((a[0], a[1], b[0], b[1], hw, clr, nid))
        self._dirty[layer] = True

    def add_circle(self, layer, x, y, r, clr, net):
        self._cir[layer].append((x, y, r, clr, self.net_id.get(net, -1)))
        self._dirty[layer] = True

    def add_polygon(self, layer, verts, clr, net):
        vx = np.ascontiguousarray([p[0] for p in verts], dtype=np.float64)
        vy = np.ascontiguousarray([p[1] for p in verts], dtype=np.float64)
        self._poly[layer].append((vx, vy, clr, self.net_id.get(net, -1)))
        self._poly_csr.pop(layer, None)

    def remove_net(self, net):
        nid = self.net_id.get(net, -1)
        for ly in self.layers:
            self._seg[ly] = [s for s in self._seg[ly] if s[6] != nid]
            self._cir[ly] = [c for c in self._cir[ly] if c[4] != nid]
            self._dirty[ly] = True

    # ---- CSR build -------------------------------------------------------
    def _cells_of_bbox(self, minx, miny, maxx, maxy):
        cxa = max(0, int((minx - self.gx0) * self.inv))
        cxb = min(self.gnx - 1, int((maxx - self.gx0) * self.inv))
        cya = max(0, int((miny - self.gy0) * self.inv))
        cyb = min(self.gny - 1, int((maxy - self.gy0) * self.inv))
        return cxa, cxb, cya, cyb

    def _build_csr(self, item_bboxes):
        """counting-sort CSR: returns (start[ncells+1], items[])."""
        counts = np.zeros(self.ncells + 1, dtype=np.int64)
        cell_lists = []
        for (minx, miny, maxx, maxy) in item_bboxes:
            cxa, cxb, cya, cyb = self._cells_of_bbox(minx, miny, maxx, maxy)
            cells = []
            for cyy in range(cya, cyb + 1):
                base = cyy * self.gnx
                for cxx in range(cxa, cxb + 1):
                    cells.append(base + cxx)
                    counts[base + cxx + 1] += 1
            cell_lists.append(cells)
        start = np.cumsum(counts)
        items = np.zeros(int(start[-1]), dtype=np.int64)
        cursor = start[:-1].copy()
        for idx, cells in enumerate(cell_lists):
            for c in cells:
                items[cursor[c]] = idx
                cursor[c] += 1
        return start.astype(np.int64), items

    def _poly_pack(self, layer):
        if layer in self._poly_csr:
            return self._poly_csr[layer]
        P = self._poly[layer]
        if P:
            pvx = np.concatenate([p[0] for p in P])
            pvy = np.concatenate([p[1] for p in P])
            poff = np.zeros(len(P) + 1, np.int64)
            for i, p in enumerate(P):
                poff[i + 1] = poff[i] + len(p[0])
            pclr = np.array([p[2] for p in P], np.float64)
            pnet = np.array([p[3] for p in P], np.int64)
            bboxes = [(p[0].min() - p[2], p[1].min() - p[2],
                       p[0].max() + p[2], p[1].max() + p[2]) for p in P]
            pstart, pitems = self._build_csr(bboxes)
        else:
            pvx = pvy = pclr = np.zeros(0)
            poff = np.zeros(1, np.int64); pnet = np.zeros(0, np.int64)
            pstart = np.zeros(self.ncells + 1, np.int64); pitems = np.zeros(0, np.int64)
        packed = (pvx, pvy, poff, pclr, pnet, pstart, pitems)
        self._poly_csr[layer] = packed
        return packed

    def _pack(self, layer):
        if not self._dirty[layer] and layer in self._csr:
            return self._csr[layer]
        S = self._seg[layer]; C = self._cir[layer]
        seg = (np.array([s[0] for s in S], np.float64) if S else np.zeros(0),
               np.array([s[1] for s in S], np.float64) if S else np.zeros(0),
               np.array([s[2] for s in S], np.float64) if S else np.zeros(0),
               np.array([s[3] for s in S], np.float64) if S else np.zeros(0),
               np.array([s[4] for s in S], np.float64) if S else np.zeros(0),
               np.array([s[5] for s in S], np.float64) if S else np.zeros(0),
               np.array([s[6] for s in S], np.int64) if S else np.zeros(0, np.int64))
        # expand each segment's index bbox by its OWN half-width + clearance
        # so a query still finds wide foreign copper whose thin centerline
        # sits a cell away
        sbb = [(min(s[0], s[2]) - s[4] - s[5], min(s[1], s[3]) - s[4] - s[5],
                max(s[0], s[2]) + s[4] + s[5], max(s[1], s[3]) + s[4] + s[5])
               for s in S]
        sstart, sitems = self._build_csr(sbb)
        cir = (np.array([c[0] for c in C], np.float64) if C else np.zeros(0),
               np.array([c[1] for c in C], np.float64) if C else np.zeros(0),
               np.array([c[2] for c in C], np.float64) if C else np.zeros(0),
               np.array([c[3] for c in C], np.float64) if C else np.zeros(0),
               np.array([c[4] for c in C], np.int64) if C else np.zeros(0, np.int64))
        cbb = [(c[0] - c[2] - c[3], c[1] - c[2] - c[3],
                c[0] + c[2] + c[3], c[1] + c[2] + c[3]) for c in C]
        cstart, citems = self._build_csr(cbb)
        packed = (seg, sstart, sitems, cir, cstart, citems)
        self._csr[layer] = packed
        self._dirty[layer] = False
        return packed

    # ---- query -----------------------------------------------------------
    def trace_ok(self, layer, coords, hw, clr, own_net, eps=-1e-6):
        if len(coords) < 2:
            return True
        seg, sstart, sitems, cir, cstart, citems = self._pack(layer)
        pvx, pvy, poff, pclr, pnet, pstart, pitems = self._poly_pack(layer)
        txs = np.ascontiguousarray([c[0] for c in coords], np.float64)
        tys = np.ascontiguousarray([c[1] for c in coords], np.float64)
        own = self.net_id.get(own_net, -1)
        return trace_ok_csr(
            txs, tys, float(hw), float(clr), own,
            seg[0], seg[1], seg[2], seg[3], seg[4], seg[5], seg[6],
            cir[0], cir[1], cir[2], cir[3], cir[4],
            pvx, pvy, poff, pclr, pnet,
            sstart, sitems, cstart, citems, pstart, pitems,
            self.gx0, self.gy0, self.inv, self.gnx, self.gny, self.ncells,
            float(eps))
