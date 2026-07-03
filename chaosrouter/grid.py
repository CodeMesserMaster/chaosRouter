"""Routing workspace: per-layer net-ownership grids + distance transforms.

owner grid semantics (int32):
    FREE  (-1) : empty routable area
    BLOCK (-2) : outside board / hard keepout
    >= 0       : copper of net with that index
"""

from __future__ import annotations

import threading

import numpy as np
import shapely
from shapely.geometry import LineString, Point

from .fields import distance_field, erode_disk
from .model import Board

FREE = -1
BLOCK = -2


class Workspace:
    def __init__(self, board: Board, step: float = 5.0, margin: float = 20.0):
        self.board = board
        self.step = step
        minx, miny, maxx, maxy = board.outline.bounds
        self.x0 = minx - margin
        self.y0 = miny - margin
        self.nx = int(np.ceil((maxx - minx + 2 * margin) / step)) + 1
        self.ny = int(np.ceil((maxy - miny + 2 * margin) / step)) + 1
        self.layers = list(board.layers)
        self.owner = {
            layer: np.full((self.ny, self.nx), FREE, dtype=np.int32) for layer in self.layers
        }
        # which net's TRACE/VIA covers a cell (pads excluded) — used by the
        # rip-up diagnosis to attribute blockage to rippable wiring
        self.wiring_owner = {
            layer: np.full((self.ny, self.nx), FREE, dtype=np.int32) for layer in self.layers
        }
        self.net_index: dict[str, int] = {name: i for i, name in enumerate(board.nets)}

        # precompute cell-center coordinate axes
        self._xs = self.x0 + np.arange(self.nx) * step
        self._ys = self.y0 + np.arange(self.ny) * step

        # exact copper registry: truth for clearance validation
        # per layer: parallel lists of (net_name | None, geometry, clearance)
        self.copper: dict[str, list] = {layer: [] for layer in self.layers}
        # exempt-mask cache per net being routed: {(net, r_cells): (masks, pad_cells, r)}
        self._exempt_cache: dict = {}
        # guards copper registry, STRtrees, owner mutation, exempt cache
        self.lock = threading.RLock()
        self._trees: dict[str, object] = {}
        self._trees_dirty = True
        from shapely.prepared import prep

        self._outline_prepared = prep(board.outline)

        self._block_outside()
        self._rasterize_pads()

    # ---- coordinate helpers -------------------------------------------
    def to_cell(self, x: float, y: float) -> tuple[int, int]:
        ix = int(round((x - self.x0) / self.step))
        iy = int(round((y - self.y0) / self.step))
        return min(max(ix, 0), self.nx - 1), min(max(iy, 0), self.ny - 1)

    def to_world(self, ix, iy):
        return self.x0 + ix * self.step, self.y0 + iy * self.step

    # ---- rasterization -------------------------------------------------
    def _cells_in_geom(self, geom, grow: float | None = None):
        """(iys, ixs) of cell centers inside geom grown by `grow`
        (default: half step, conservative for obstacles)."""
        if grow is None:
            grow = self.step * 0.5
        g = geom.buffer(grow) if grow else geom
        minx, miny, maxx, maxy = g.bounds
        ix0 = max(0, int(np.floor((minx - self.x0) / self.step)))
        ix1 = min(self.nx - 1, int(np.ceil((maxx - self.x0) / self.step)))
        iy0 = max(0, int(np.floor((miny - self.y0) / self.step)))
        iy1 = min(self.ny - 1, int(np.ceil((maxy - self.y0) / self.step)))
        if ix1 < ix0 or iy1 < iy0:
            return np.empty(0, int), np.empty(0, int)
        xs = self._xs[ix0 : ix1 + 1]
        ys = self._ys[iy0 : iy1 + 1]
        gx, gy = np.meshgrid(xs, ys)
        mask = shapely.contains_xy(g, gx.ravel(), gy.ravel()).reshape(gy.shape)
        iys, ixs = np.nonzero(mask)
        return iys + iy0, ixs + ix0

    def _block_outside(self):
        """Mark cells outside the board outline (shrunk by margin) as BLOCK."""
        inside = self.board.outline.buffer(-self.step)
        gx, gy = np.meshgrid(self._xs, self._ys)
        mask = shapely.contains_xy(inside, gx.ravel(), gy.ravel()).reshape(gy.shape)
        self._outside = ~mask  # kept for regional rebuilds after rip-up
        for layer in self.layers:
            self.owner[layer][self._outside] = BLOCK

    def _rasterize_pads(self):
        for pad in self.board.pads.values():
            nid = self.net_index.get(pad.net, BLOCK)  # no-net pads block everyone
            net = self.board.nets.get(pad.net) if pad.net else None
            clr = net.clearance if net else self.board.default_clearance
            for layer in self.layers:
                if layer not in pad.layers():
                    continue
                geom = pad.geometry_on(layer)
                if geom is None:
                    continue
                iys, ixs = self._cells_in_geom(geom)
                self.owner[layer][iys, ixs] = nid
                self.copper[layer].append((pad.net, geom, clr, "pad", geom, 0.0))

    # ---- exact clearance validation ------------------------------------
    def _tree(self, layer):
        """(STRtree, consistent items snapshot) for a layer. Thread-safe:
        the snapshot is taken at build time, so indices always match."""
        from shapely.strtree import STRtree

        with self.lock:
            if self._trees_dirty:
                self._trees = {}
                for ly in self.layers:
                    items = list(self.copper[ly])
                    tree = STRtree([c[1] for c in items]) if items else None
                    self._trees[ly] = (tree, items)
                self._trees_dirty = False
            return self._trees.get(layer, (None, []))

    # Clearance gaps are measured on TRUE geometry: centerline/center-point
    # distance minus radii — mathematically exact for round-capped traces
    # and circular vias (what DipTrace measures), no polygonization error.
    # Copper registry items are (net, poly, clr, kind, raw, r): poly for
    # STRtree/rasterizing, raw+r for exact gap math.

    def _edge_tree(self):
        """STRtree over board-outline boundary segments (lazy)."""
        from shapely.geometry import LineString
        from shapely.strtree import STRtree

        with self.lock:
            if not hasattr(self, "_edge_tree_items"):
                segs = []
                boundary = self.board.outline.boundary
                for geom in getattr(boundary, "geoms", [boundary]):
                    c = list(geom.coords)
                    segs += [LineString([c[i], c[i + 1]]) for i in range(len(c) - 1)]
                self._edge_tree_items = (STRtree(segs), segs)
            return self._edge_tree_items

    def edge_clear(self, raw, r: float, clearance: float) -> bool:
        """True if copper (raw geometry grown by r) keeps `clearance` to
        the board edge."""
        from shapely.geometry import box

        b = raw.bounds
        m = r + clearance + 0.5
        tree, segs = self._edge_tree()
        for idx in tree.query(box(b[0] - m, b[1] - m, b[2] + m, b[3] + m)):
            if raw.distance(segs[int(idx)]) - r < clearance - 1e-6:
                return False
        return True

    def exact_trace_ok(
        self, net: str, layer: str, coords, width: float, clearance: float,
        friends: frozenset = frozenset(),
    ) -> bool:
        """True if the trace keeps exact clearance to all foreign copper
        and to the board edge. `friends` are additional nets exempt from
        the check (e.g. the partner of a differential pair)."""
        from shapely.geometry import LineString, box

        line = LineString(coords)
        half = width / 2
        poly = line.buffer(half, quad_segs=16)
        if not self._outline_prepared.contains(poly):
            return False
        if not self.edge_clear(line, half, clearance):
            return False
        tree, items = self._tree(layer)
        if tree is None:
            return True
        b = line.bounds
        m = half + clearance + 0.5
        for idx in tree.query(box(b[0] - m, b[1] - m, b[2] + m, b[3] + m)):
            other_net, geom, other_clr, kind, raw, r = items[int(idx)]
            if other_net is not None and other_net == net:
                continue
            # partner nets of a diff pair are exempt only for their TRACES
            # (parallel by construction) — their pads are real copper obstacles
            if other_net in friends and kind in ("trace", "via"):
                continue
            if line.distance(raw) - half - r < max(clearance, other_clr) - 1e-6:
                return False
        return True

    def exact_via_ok(self, net: str, x: float, y: float, diameter: float, clearance: float) -> bool:
        """True if via keeps exact clearance to foreign copper on all layers
        AND does not overlap any pad (no via-in-pad), own net included."""
        from shapely.geometry import Point, box

        pt = Point(x, y)
        rad = diameter / 2
        disc = pt.buffer(rad, quad_segs=16)
        if not self._outline_prepared.contains(disc):
            return False
        if not self.edge_clear(pt, rad, clearance):
            return False
        b = (x - rad, y - rad, x + rad, y + rad)
        for layer in self.layers:
            tree, items = self._tree(layer)
            if tree is None:
                continue
            m = rad + clearance + 0.5
            for idx in tree.query(box(x - m, y - m, x + m, y + m)):
                other_net, geom, other_clr, kind, raw, r = items[int(idx)]
                gap = pt.distance(raw) - rad - r
                if kind == "pad":
                    if other_net == net and other_net is not None:
                        # same net: no via-in-pad, and keep a visible gap,
                        # but full clearance isn't electrically required
                        if gap < 2.0 - 1e-6:
                            return False
                    elif gap < max(clearance, other_clr) - 1e-6:
                        return False
                    continue
                if other_net == net and other_net is not None:
                    continue
                if gap < max(clearance, other_clr) - 1e-6:
                    return False
        return True

    # ---- dynamic obstacles ----------------------------------------------
    def add_trace(self, net: str, layer: str, coords, width: float):
        line = LineString(coords)
        geom = line.buffer(width / 2, quad_segs=8)
        with self.lock:
            nid = self.net_index[net]
            clr = self.board.nets[net].clearance
            iys, ixs = self._cells_in_geom(geom)
            # don't overwrite foreign pads/copper — only claim free cells + own
            grid = self.owner[layer]
            sel = (grid[iys, ixs] == FREE) | (grid[iys, ixs] == nid)
            grid[iys[sel], ixs[sel]] = nid
            self.wiring_owner[layer][iys, ixs] = nid
            self.copper[layer].append((net, geom, clr, "trace", line, width / 2))
            self._trees_dirty = True
            self._patch_exempt(net, geom, [layer])

    def add_via(self, net: str, x: float, y: float, diameter: float):
        pt = Point(x, y)
        geom = pt.buffer(diameter / 2, quad_segs=8)
        with self.lock:
            nid = self.net_index[net]
            clr = self.board.nets[net].clearance
            iys, ixs = self._cells_in_geom(geom)
            for layer in self.layers:
                grid = self.owner[layer]
                sel = (grid[iys, ixs] == FREE) | (grid[iys, ixs] == nid)
                grid[iys[sel], ixs[sel]] = nid
                self.wiring_owner[layer][iys, ixs] = nid
                self.copper[layer].append((net, geom, clr, "via", pt, diameter / 2))
            self._trees_dirty = True
            self._patch_exempt(net, geom, self.layers)

    def remove_net_wiring(self, net: str):
        """Rip a net's traces and vias (pads stay). Owner cells they covered
        are rebuilt from the remaining exact copper registry."""
        with self.lock:
            removed = []
            for layer in self.layers:
                keep = []
                for item in self.copper[layer]:
                    if item[0] == net and item[3] in ("trace", "via"):
                        removed.append((layer, item[1]))
                    else:
                        keep.append(item)
                self.copper[layer] = keep
            self._trees_dirty = True
            self._exempt_cache = {}  # rip events are rare; drop the cache
            for layer, geom in removed:
                self._rebuild_region(layer, geom)
            return len(removed)

    def drop_exempt(self, net: str):
        """Free a finished net's cached exempt masks."""
        with self.lock:
            for key in [k for k in self._exempt_cache if k[0] == net]:
                del self._exempt_cache[key]

    def _rebuild_region(self, layer: str, geom):
        """Reset owner cells under geom, then re-apply overlapping copper."""
        iys, ixs = self._cells_in_geom(geom)
        if len(iys) == 0:
            return
        grid = self.owner[layer]
        wgrid = self.wiring_owner[layer]
        grid[iys, ixs] = np.where(self._outside[iys, ixs], BLOCK, FREE)
        wgrid[iys, ixs] = FREE
        minx, miny, maxx, maxy = geom.buffer(self.step).bounds
        for other_net, g, clr, kind, _raw, _r in self.copper[layer]:
            b = g.bounds
            if b[0] > maxx or b[2] < minx or b[1] > maxy or b[3] < miny:
                continue
            nid = self.net_index.get(other_net, BLOCK) if other_net else BLOCK
            oys, oxs = self._cells_in_geom(g)
            sel = ~self._outside[oys, oxs]
            grid[oys[sel], oxs[sel]] = nid
            if kind in ("trace", "via") and nid >= 0:
                wgrid[oys[sel], oxs[sel]] = nid

    def pad_distance_layer(self) -> dict:
        """Per-layer distance (mils) to the nearest pad ON THAT LAYER —
        drives the keep-away-from-pad-fields transit penalty. Cached."""
        if not hasattr(self, "_pad_dist_layer"):
            out = {}
            for layer in self.layers:
                mask = np.zeros((self.ny, self.nx), dtype=bool)
                for net, geom, clr, kind, _raw, _r in self.copper[layer]:
                    if kind != "pad":
                        continue
                    iys, ixs = self._cells_in_geom(geom)
                    mask[iys, ixs] = True
                out[layer] = distance_field(mask, self.step)
            self._pad_dist_layer = out
        return self._pad_dist_layer

    def pad_distance(self) -> np.ndarray:
        """Distance (mils) from each cell to the nearest pad copper on ANY
        layer — used to forbid via-in-pad placements. Cached."""
        if not hasattr(self, "_pad_dist"):
            mask = np.zeros((self.ny, self.nx), dtype=bool)
            for layer in self.layers:
                for net, geom, clr, kind, _raw, _r in self.copper[layer]:
                    if kind != "pad":
                        continue
                    iys, ixs = self._cells_in_geom(geom)
                    mask[iys, ixs] = True
            self._pad_dist = distance_field(mask, self.step)
        return self._pad_dist

    # ---- clearance fields -------------------------------------------------
    EDT_GUARD = 60.0  # mil of extra visibility beyond a windowed region

    def _edt_layers(self, masks: dict) -> dict:
        """EDT per layer, on parallel threads (the kernel is GIL-free)."""
        if len(masks) <= 1:
            return {ly: distance_field(m, self.step) for ly, m in masks.items()}
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(masks)) as pool:
            futs = {
                ly: pool.submit(distance_field, m, self.step)
                for ly, m in masks.items()
            }
            return {ly: f.result() for ly, f in futs.items()}

    def foreign_distance(self, net, bounds=None) -> dict[str, np.ndarray]:
        """Per layer: distance (in mils) from each cell to nearest foreign copper
        or board edge. `net` may be one net name or an iterable of friendly nets
        (e.g. both nets of a differential pair).

        With `bounds` (minx, miny, maxx, maxy in mils) the field is computed
        only for that region (plus a guard ring); cells outside read 0.0,
        i.e. blocked — callers must keep their search windows inside."""
        nets = {net} if isinstance(net, str) else set(net)
        nids = np.array(sorted(self.net_index[n] for n in nets))

        if bounds is not None:
            g = self.EDT_GUARD
            gx0, gy0 = self.to_cell(bounds[0] - g, bounds[1] - g)
            gx1, gy1 = self.to_cell(bounds[2] + g, bounds[3] + g)
            ix0, iy0 = self.to_cell(bounds[0], bounds[1])
            ix1, iy1 = self.to_cell(bounds[2], bounds[3])

        masks = {}
        for layer in self.layers:
            grid = self.owner[layer]
            if bounds is not None:
                grid = grid[gy0 : gy1 + 1, gx0 : gx1 + 1]
            foreign = (grid == BLOCK) | ((grid >= 0) & ~np.isin(grid, nids))
            masks[layer] = np.ascontiguousarray(foreign)
        fields = self._edt_layers(masks)
        if bounds is None:
            return fields
        out = {}
        for layer, d in fields.items():
            full = np.zeros((self.ny, self.nx), dtype=np.float32)
            full[iy0 : iy1 + 1, ix0 : ix1 + 1] = d[
                iy0 - gy0 : iy1 - gy0 + 1, ix0 - gx0 : ix1 - gx0 + 1
            ]
            out[layer] = full
        return out

    def own_mask(self, net: str) -> dict[str, np.ndarray]:
        nid = self.net_index[net]
        return {layer: self.owner[layer] == nid for layer in self.layers}

    def own_exempt_mask(
        self, net: str, width: float, bounds=None
    ) -> dict[str, np.ndarray]:
        """Cells where a trace centerline of `width` may ignore the clearance
        field because the full trace body provably stays inside own copper.

        Own traces: raster mask eroded by (width/2 + half step).
        Own pads: exact polygon shrunk by width/2 (escape corridor), plus the
        pad center cell as a guaranteed escape point.

        Cached per net; add_trace/add_via patch the cache regionally."""
        r_cells = int(np.ceil((width / 2 + 0.5 * self.step) / self.step))
        key = (net, r_cells, bounds is None)
        with self.lock:
            hit = self._exempt_cache.get(key)
        if hit is not None:
            return hit[0]

        nid = self.net_index[net]
        out = {}
        if bounds is not None:
            g = self.EDT_GUARD
            gx0, gy0 = self.to_cell(bounds[0] - g, bounds[1] - g)
            gx1, gy1 = self.to_cell(bounds[2] + g, bounds[3] + g)
            for layer in self.layers:
                sub = np.ascontiguousarray(
                    self.owner[layer][gy0 : gy1 + 1, gx0 : gx1 + 1] == nid
                )
                full = np.zeros((self.ny, self.nx), dtype=bool)
                full[gy0 : gy1 + 1, gx0 : gx1 + 1] = erode_disk(sub, r_cells)
                out[layer] = full
        else:
            for layer in self.layers:
                out[layer] = erode_disk(self.owner[layer] == nid, r_cells)
        pad_cells = {layer: [[], []] for layer in self.layers}
        for pad in self.board.pads.values():
            if pad.net != net:
                continue
            ix, iy = self.to_cell(pad.x, pad.y)
            for layer in self.layers:
                if layer not in pad.layers():
                    continue
                geom = pad.geometry_on(layer)
                if geom is not None:
                    shrunk = geom.buffer(-width / 2 - 0.05)
                    if not shrunk.is_empty:
                        iys, ixs = self._cells_in_geom(shrunk, grow=0)
                        out[layer][iys, ixs] = True
                        pad_cells[layer][0].extend(iys.tolist())
                        pad_cells[layer][1].extend(ixs.tolist())
                out[layer][iy, ix] = True
                pad_cells[layer][0].append(iy)
                pad_cells[layer][1].append(ix)
        pad_cells = {
            layer: (np.array(v[0], dtype=int), np.array(v[1], dtype=int))
            for layer, v in pad_cells.items()
        }
        with self.lock:
            self._exempt_cache[key] = (out, pad_cells, r_cells)
        return out

    def _patch_exempt(self, net: str, geom, layers):
        """Regionally refresh the cached exempt mask after own copper grew.
        Caller holds self.lock."""
        for key, (masks, pad_cells, r) in self._exempt_cache.items():
            if key[0] != net:
                continue
            nid = self.net_index[net]
            minx, miny, maxx, maxy = geom.bounds
            ix0 = max(0, int((minx - self.x0) / self.step) - r - 2)
            ix1 = min(self.nx - 1, int((maxx - self.x0) / self.step) + r + 3)
            iy0 = max(0, int((miny - self.y0) / self.step) - r - 2)
            iy1 = min(self.ny - 1, int((maxy - self.y0) / self.step) + r + 3)
            ex0, ex1 = max(0, ix0 - r), min(self.nx - 1, ix1 + r)
            ey0, ey1 = max(0, iy0 - r), min(self.ny - 1, iy1 + r)
            for layer in layers:
                sub = self.owner[layer][ey0 : ey1 + 1, ex0 : ex1 + 1] == nid
                ero = erode_disk(np.ascontiguousarray(sub), r)
                masks[layer][iy0 : iy1 + 1, ix0 : ix1 + 1] = ero[
                    iy0 - ey0 : iy1 - ey0 + 1, ix0 - ex0 : ix1 - ex0 + 1
                ]
                pys, pxs = pad_cells[layer]
                if len(pys):
                    masks[layer][pys, pxs] = True

    def line_cells(self, coords) -> tuple[np.ndarray, np.ndarray]:
        """(iys, ixs) of cells a polyline's centerline passes through."""
        pts = []
        for (x1, y1), (x2, y2) in zip(coords[:-1], coords[1:]):
            n = max(2, int(np.hypot(x2 - x1, y2 - y1) / (self.step * 0.5)) + 1)
            for t in np.linspace(0, 1, n):
                pts.append(self.to_cell(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        if not pts:
            return np.empty(0, int), np.empty(0, int)
        arr = np.unique(np.array(pts), axis=0)
        return arr[:, 1], arr[:, 0]
