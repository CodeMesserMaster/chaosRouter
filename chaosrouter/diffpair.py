"""Differential pair routing: one curved centerline, two parallel offsets.

Each pair is decomposed into 'stations' (a P pad and N pad sitting together,
e.g. the two pins of a transceiver / filter / TVS). Stations are chained into
the shortest open path, and every consecutive station pair is routed as a
single fat 'envelope' path (2*width + gap wide). The centerline is filleted
into arcs, then both member traces are generated as exact parallel offset
curves — coupled everywhere except the short breakouts at the pads.
"""

from __future__ import annotations

import math
import re
from itertools import permutations

import numpy as np
from shapely.geometry import LineString

from .curves import fillet_polyline
from .router import Trace

# extra edge gap over the class minimum, so arc chord sampling never dips
# the P-N distance below the DRC clearance
GAP_SLACK = 0.25

MODULE_PENALTY = 4.0  # per-cell cost multiple of step inside module regions
MODULE_MIN_PINS = 12
MODULE_MARGIN = 40.0  # mil around a module's bbox


def module_avoid_mask(router):
    """Cells occupied by large components (+margin). Pairs prefer to route
    AROUND modules — the veteran workflow — leaving pad fields for escapes."""
    if getattr(router, "_module_mask", None) is not None:
        return router._module_mask
    ws = router.ws
    from collections import defaultdict

    by_ref = defaultdict(list)
    for p in router.board.pads.values():
        by_ref[p.ref].append(p)
    mask = np.zeros((ws.ny, ws.nx), dtype=bool)
    for ref, pads in by_ref.items():
        if len(pads) < MODULE_MIN_PINS:
            continue
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        ix0, iy0 = ws.to_cell(min(xs) - MODULE_MARGIN, min(ys) - MODULE_MARGIN)
        ix1, iy1 = ws.to_cell(max(xs) + MODULE_MARGIN, max(ys) + MODULE_MARGIN)
        mask[iy0 : iy1 + 1, ix0 : ix1 + 1] = True
    router._module_mask = mask
    return mask


def find_diff_pairs(board):
    """[(net_P, net_N, gap_mil), ...] detected from net classes."""
    pairs = []
    for cls in board.classes.values():
        if "edge_primary_gap" not in cls.rules and "diffpair_line_width" not in cls.rules:
            continue
        gap_rule = cls.rules.get("edge_primary_gap")
        gap = float(gap_rule[0]) if gap_rule else cls.clearance
        by_stem: dict[str, dict] = {}
        for name in cls.nets:
            m = re.match(r"(.*?)_?([PN])$", name)
            if m:
                by_stem.setdefault(m.group(1), {})[m.group(2)] = name
        for stem, d in by_stem.items():
            if "P" in d and "N" in d and d["P"] in board.nets and d["N"] in board.nets:
                pairs.append((board.nets[d["P"]], board.nets[d["N"]], gap))
    return pairs


def _stations(board, net_p, net_n):
    """Match each P pad with its nearest N pad -> [(pad_p, pad_n), ...]."""
    pads_p = board.pads_of_net(net_p)
    pads_n = list(board.pads_of_net(net_n))
    if len(pads_p) != len(pads_n) or not pads_p:
        return None
    stations = []
    for pp in pads_p:
        pn = min(pads_n, key=lambda q: math.hypot(q.x - pp.x, q.y - pp.y))
        pads_n.remove(pn)
        stations.append((pp, pn))
    return stations


def _mid(st):
    return ((st[0].x + st[1].x) / 2, (st[0].y + st[1].y) / 2)


def _best_chain(stations):
    """Order stations into the shortest open path."""
    if len(stations) <= 2:
        return stations
    mids = [_mid(s) for s in stations]
    d = lambda i, j: math.hypot(mids[i][0] - mids[j][0], mids[i][1] - mids[j][1])
    best, best_len = None, float("inf")
    for perm in permutations(range(len(stations))):
        if perm[0] > perm[-1]:
            continue  # skip mirrored duplicates
        total = sum(d(perm[k], perm[k + 1]) for k in range(len(perm) - 1))
        if total < best_len:
            best, best_len = perm, total
    return [stations[i] for i in best]


STANDOFF = 35.0  # mil of uncoupled breakout at each station (rules allow 393)
MIN_COUPLE = 110.0  # station spans shorter than this route uncoupled


def route_diff_pair(router, net_p, net_n, gap: float) -> bool:
    """Route one differential pair as chained coupled segments.

    The whole pair is one transaction per layer plan: try the coupled runs
    on inner layers first (outer layers stay free for pad escapes); if ANY
    leg or breakout fails, undo the whole pair and retry outer-first. Only
    if both plans fail is the pair recorded for pair-aware rip-up — pairs
    never silently decouple. Short legs (< MIN_COUPLE) route individually,
    which the rules' uncoupled-length allowance permits."""
    board, ws = router.board, router.ws
    stations = _stations(board, net_p, net_n)
    if not stations or len(stations) < 2:
        return False
    chain = _best_chain(stations)

    inner = list(ws.layers[1:-1])
    outer = [ws.layers[0], ws.layers[-1]] if len(ws.layers) > 1 else list(ws.layers)
    plans = [inner + outer, outer + inner] if inner else [outer]

    for plan in plans:
        if _route_pair_attempt(router, net_p, net_n, gap, chain, plan):
            return True
        # transactional undo: remove everything this attempt laid down
        router._rip_net(net_p.name)
        router._rip_net(net_n.name)

    # both plans failed: record for pair-aware rip-up
    for st_a, st_b in zip(chain[:-1], chain[1:]):
        for net, pad_a, pad_b in (
            (net_p, st_a[0], st_b[0]),
            (net_n, st_a[1], st_b[1]),
        ):
            router.result.failed.append((net.name, pad_a.pin_id, pad_b.pin_id))
    router.result.pair_segments_failed.append(
        (net_p.name, net_n.name, gap, chain[0][0].pin_id, chain[-1][0].pin_id)
    )
    return True


def _route_pair_attempt(router, net_p, net_n, gap, chain, layer_order) -> bool:
    """One all-or-nothing routing attempt for the whole pair."""
    ws = router.ws
    width = net_p.width
    pitch = width + gap + GAP_SLACK
    env_width = 2 * width + gap + GAP_SLACK
    clearance = net_p.clearance
    friends = frozenset({net_p.name, net_n.name})
    dist = ws.foreign_distance(friends)

    for st_a, st_b in zip(chain[:-1], chain[1:]):
        a_mid, b_mid = _mid(st_a), _mid(st_b)
        span = math.hypot(b_mid[0] - a_mid[0], b_mid[1] - a_mid[1])
        if span < MIN_COUPLE:
            for net, pad_a, pad_b in (
                (net_p, st_a[0], st_b[0]),
                (net_n, st_a[1], st_b[1]),
            ):
                if not _route_individual(router, net, pad_a, pad_b):
                    return False
            continue

        seg = _route_pair_segment(
            router, st_a, st_b, net_p, net_n, width, pitch, env_width,
            clearance, friends, dist, layer_order,
        )
        if seg is None:
            return False
        layer, coords_p, coords_n, _, _ = seg
        for net, coords in ((net_p, coords_p), (net_n, coords_n)):
            router.result.traces.append(
                Trace(net.name, layer, coords, width, no_fillet=True)
            )
            ws.add_trace(net.name, layer, coords, width)
            router.result.routed_edges += 1
            router.result.edges_by_net[net.name] = (
                router.result.edges_by_net.get(net.name, 0) + 1
            )
        for net, coords in ((net_p, coords_p), (net_n, coords_n)):
            pad_a = st_a[0] if net is net_p else st_a[1]
            pad_b = st_b[0] if net is net_p else st_b[1]
            for pad, tip in ((pad_a, coords[0]), (pad_b, coords[-1])):
                if not _route_breakout(router, net, pad, tip, coords, layer):
                    return False
    return True


def _register(router, net, out):
    ws = router.ws
    runs, vias = out
    via_name, via_dia = router.via_for(net)
    for run_layer, coords, width in runs:
        if len(coords) >= 2:
            router.result.traces.append(Trace(net.name, run_layer, coords, width))
            ws.add_trace(net.name, run_layer, coords, width)
    for x, y in vias:
        from .router import Via

        router.result.vias.append(Via(net.name, x, y, via_dia, padstack=via_name))
        ws.add_via(net.name, x, y, via_dia)
    router.result.routed_edges += 1
    router.result.edges_by_net[net.name] = router.result.edges_by_net.get(net.name, 0) + 1


def _route_individual(router, net, pad_a, pad_b) -> bool:
    """Plain single-net connection pad_a -> pad_b via the normal machinery."""
    ws = router.ws
    target = {ly: np.zeros((ws.ny, ws.nx), dtype=bool) for ly in ws.layers}
    centers = []
    cx, cy = ws.to_cell(pad_b.x, pad_b.y)
    for li, layer in enumerate(ws.layers):
        if layer not in pad_b.layers():
            continue
        geom = pad_b.geometry_on(layer)
        if geom is not None:
            iys, ixs = ws._cells_in_geom(geom, grow=0)
            target[layer][iys, ixs] = True
        target[layer][cy, cx] = True
        centers.append((li, cy, cx))

    dist = ws.foreign_distance(net.name)
    own = ws.own_exempt_mask(net.name, net.width)
    out = router._route_to_tree(net, pad_a, (pad_b.x, pad_b.y), target, centers, dist, own)
    if out is None:
        return False
    _register(router, net, out)
    return True


def _route_breakout(router, net, pad, tip, seg_coords, layer) -> bool:
    """Route pad -> this coupled segment (target = the segment's own cells)."""
    from shapely.geometry import Point

    ws = router.ws
    li = ws.layers.index(layer)
    target = {ly: np.zeros((ws.ny, ws.nx), dtype=bool) for ly in ws.layers}
    # target ONLY the segment end near this pad: joining mid-segment would
    # leave the coupled trace's tip dangling as an open stub
    from shapely.geometry import Point as _P
    from shapely.ops import substring as _substring

    seg_line = LineString(seg_coords)
    d = seg_line.project(_P(tip))
    zone = _substring(seg_line, max(0.0, d - 25.0), min(seg_line.length, d + 25.0))
    zone_coords = list(zone.coords) if zone.geom_type == "LineString" else [tip, tip]
    iys, ixs = ws.line_cells(zone_coords)
    target[layer][iys, ixs] = True
    centers = [(li, int(iy), int(ix)) for iy, ix in zip(iys, ixs)]

    dist = ws.foreign_distance(net.name)
    own = ws.own_exempt_mask(net.name, net.width)
    out = router._route_to_tree(
        net, pad, tip, target, centers, dist, own,
        windows=(150.0, 400.0, 1000.0), snap_line=seg_line,
    )
    if out is None:
        # ROBUST fallback: a pad in a crowded field (e.g. a Top connector pad)
        # may need to escape on its own layer and via DOWN to the segment AWAY
        # from the congestion, not with a via jammed at the pad. Retry with no
        # snap constraint, a full-board window and cheap vias so the via lands
        # wherever there IS room. Beats rolling back the whole coupled pair for
        # one stubborn breakout.
        saved_via = router.via_cost
        router.via_cost = min(router.via_cost, 60.0)
        try:
            out = router._route_to_tree(
                net, pad, tip, target, centers, dist, own,
                windows=(150.0, 400.0, 1000.0, 1e9), snap_line=None,
            )
        finally:
            router.via_cost = saved_via
        if out is None:
            return False
    _register(router, net, out)
    return True


def _route_pair_segment(
    router, st_a, st_b, net_p, net_n, width, pitch, env_width, clearance,
    friends, dist, layer_order,
):
    from shapely.ops import substring

    ws = router.ws
    ap, an = st_a
    bp, bn = st_b

    a_mid, b_mid = _mid(st_a), _mid(st_b)

    center = None
    layer = None
    for cand in layer_order:
        def center_clear(arc_pts, _l=cand):
            return ws.exact_trace_ok(
                net_p.name, _l, arc_pts, env_width, clearance, friends=friends
            )

        center = _route_envelope(
            router, a_mid, b_mid, cand, env_width, clearance, dist, center_clear
        )
        if center is not None and len(center) >= 2:
            center = fillet_polyline(center, r_target=20.0, is_clear=center_clear)
            if len(center) >= 2:
                layer = cand
                break
            center = None
    if center is None or layer is None:
        return None

    def center_clear(arc_pts, _l=layer):
        return ws.exact_trace_ok(
            net_p.name, _l, arc_pts, env_width, clearance, friends=friends
        )

    # trim back from the stations until the coupled traces exactly clear all
    # pads (partner pads are real copper; only partner traces are exempt)
    full = LineString(center)
    for trim in (STANDOFF, 60.0, 90.0, 130.0, 180.0):
        if full.length - 2 * trim < 10.0:
            return None
        line = substring(full, trim, full.length - trim)
        if line.geom_type != "LineString" or line.length < 5.0:
            return None

        off_l = line.offset_curve(pitch / 2, join_style="round")
        off_r = line.offset_curve(-pitch / 2, join_style="round")
        if (
            off_l.is_empty or off_r.is_empty
            or off_l.geom_type != "LineString" or off_r.geom_type != "LineString"
        ):
            return None

        c0, c1 = list(line.coords)[0], list(line.coords)[1]
        dx, dy = c1[0] - c0[0], c1[1] - c0[1]
        cross = dx * (ap.y - a_mid[1]) - dy * (ap.x - a_mid[0])
        p_curve, n_curve = (off_l, off_r) if cross > 0 else (off_r, off_l)

        def oriented(pad_a, curve):
            pts = list(curve.coords)
            if math.hypot(pts[0][0] - pad_a.x, pts[0][1] - pad_a.y) > math.hypot(
                pts[-1][0] - pad_a.x, pts[-1][1] - pad_a.y
            ):
                pts.reverse()
            return pts

        coords_p = oriented(ap, p_curve)
        coords_n = oriented(an, n_curve)

        if all(
            ws.exact_trace_ok(net.name, layer, coords, width, clearance, friends=friends)
            for net, coords in ((net_p, coords_p), (net_n, coords_n))
        ):
            return layer, coords_p, coords_n, st_a, st_b
    return None


def _route_envelope(router, a_mid, b_mid, layer, env_width, clearance, dist, exact_clear):
    """A* for the pair centerline on a single layer (no vias).
    exact_clear(pts) validates an envelope polyline exactly."""
    import heapq

    ws = router.ws
    step = ws.step
    d_layer = dist[layer]

    ax, ay = ws.to_cell(*a_mid)
    bx, by = ws.to_cell(*b_mid)

    for margin_mil in (200.0, 600.0, 1e9):
        m = int(margin_mil / step)
        x0, x1 = max(0, min(ax, bx) - m), min(ws.nx - 1, max(ax, bx) + m)
        y0, y1 = max(0, min(ay, by) - m), min(ws.ny - 1, max(ay, by) + m)
        for edt_margin in (router.OPT_MARGIN, router.edt_margin):
            req = env_width / 2 + clearance + edt_margin * step
            wx, wy = x1 - x0 + 1, y1 - y0 + 1

            trav = d_layer[y0 : y1 + 1, x0 : x1 + 1] >= req
            sx, sy = ax - x0, ay - y0
            gx, gy = bx - x0, by - y0
            trav[sy, sx] = trav[gy, gx] = True
            # veteran rule: pairs go AROUND big modules, not through them
            avoid = module_avoid_mask(router)[y0 : y1 + 1, x0 : x1 + 1]
            pen = MODULE_PENALTY * step

            SQRT2 = math.sqrt(2)
            moves = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
            gcost = np.full(wy * wx, np.inf)
            parent = np.full(wy * wx, -1, dtype=np.int64)
            h = lambda iy, ix: math.hypot(ix - gx, iy - gy) * step
            start, goal = sy * wx + sx, gy * wx + gx
            gcost[start] = 0.0
            heap = [(h(sy, sx), start)]
            trav_flat = trav.reshape(-1)
            avoid_flat = avoid.reshape(-1)
            found = False
            while heap:
                f, s = heapq.heappop(heap)
                iy, ix = divmod(s, wx)
                if s == goal:
                    found = True
                    break
                g = gcost[s]
                if f > g + h(iy, ix) + 1e-9:
                    continue
                for mdx, mdy in moves:
                    nxx, nyy = ix + mdx, iy + mdy
                    if nxx < 0 or nyy < 0 or nxx >= wx or nyy >= wy:
                        continue
                    ns = s + mdy * wx + mdx
                    if not trav_flat[ns]:
                        continue
                    ng = g + (step if mdx == 0 or mdy == 0 else step * SQRT2)
                    if avoid_flat[ns]:
                        ng += pen
                    if ng < gcost[ns] - 1e-9:
                        gcost[ns] = ng
                        parent[ns] = s
                        heapq.heappush(heap, (ng + h(nyy, nxx), ns))
            if not found:
                continue
            cells = []
            s = goal
            while s >= 0:
                iy, ix = divmod(s, wx)
                cells.append((iy + y0, ix + x0))
                s = parent[s]
            cells.reverse()
            pts = [ws.to_world(ix, iy) for iy, ix in cells]
            pts[0], pts[-1] = a_mid, b_mid
            no_own = np.zeros((ws.ny, ws.nx), dtype=bool)
            pts = router._string_pull(
                pts, d_layer, no_own, req,
                exact_check=lambda p, q: exact_clear([p, q]),
            )
            # endpoints sit between the pair's own pads and may fail the fat
            # envelope test there — validate the interior strictly, ends loosely
            if exact_clear(pts if len(pts) < 4 else pts[1:-1]):
                return pts
            continue  # try pessimistic margin / bigger window
        if x0 == 0 and y0 == 0 and x1 == ws.nx - 1 and y1 == ws.ny - 1:
            break
    return None
