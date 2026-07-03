"""Independent design-rule check on final routed geometry (shapely-based).

This deliberately does NOT reuse the router's grid — it checks the exact
polygons, so it catches quantization errors the grid could hide.
"""

from __future__ import annotations

from collections import defaultdict

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree


def check(board, result, connectivity: bool = True):
    """Returns (violations, opens). violations: list of dicts.

    Gaps are measured on TRUE geometry (centerline distance minus radii),
    exact for round-capped traces and circular vias — matches DipTrace."""
    items = []  # (layer, net, poly, clearance, label, raw, r)

    for pad in board.pads.values():
        for layer in pad.layers():
            geom = pad.geometry_on(layer)
            if geom is None:
                continue
            net = board.nets.get(pad.net) if pad.net else None
            clr = net.clearance if net else board.default_clearance
            items.append((layer, pad.net, geom, clr, f"pad {pad.pin_id}", geom, 0.0))

    for t in result.traces:
        net = board.nets[t.net]
        line = LineString(t.coords)
        geom = line.buffer(t.width / 2, quad_segs=16)
        items.append(
            (t.layer, t.net, geom, net.clearance, f"trace {t.net}", line, t.width / 2)
        )

    for v in result.vias:
        net = board.nets[v.net]
        pt = Point(v.x, v.y)
        geom = pt.buffer(v.diameter / 2, quad_segs=16)
        for layer in board.layers:
            items.append(
                (layer, v.net, geom, net.clearance, f"via {v.net}", pt, v.diameter / 2)
            )

    violations = []
    # copper (ours) vs board edge: DipTrace flags trace/via too close to
    # the outline, so we must too
    edge = board.outline.boundary
    seen_edge = set()
    for layer, net, geom, clr, lab, raw, r in items:
        if lab.startswith("pad") or id(raw) in seen_edge:
            continue  # pads are pre-existing; vias repeat per layer
        seen_edge.add(id(raw))
        gap = raw.distance(edge) - r
        if gap < clr - 1e-6:
            violations.append(
                {
                    "layer": layer, "a": lab, "b": "board edge",
                    "gap": round(gap, 3), "need": round(clr, 3),
                    "where": tuple(round(c, 1) for c in geom.centroid.coords[0]),
                }
            )
    by_layer = defaultdict(list)
    for idx, item in enumerate(items):
        by_layer[item[0]].append(idx)

    for layer, idxs in by_layer.items():
        geoms = [items[i][2] for i in idxs]
        tree = STRtree(geoms)
        for local_i, i in enumerate(idxs):
            _, net_i, g_i, clr_i, lab_i, raw_i, r_i = items[i]
            hits = tree.query(g_i.buffer(clr_i + 0.5))
            for local_j in hits:
                j = idxs[int(local_j)]
                if j <= i:
                    continue
                _, net_j, g_j, clr_j, lab_j, raw_j, r_j = items[j]
                if net_i == net_j and net_i is not None:
                    continue
                if lab_i.startswith("pad") and lab_j.startswith("pad"):
                    continue  # pre-existing board geometry, not ours
                gap = raw_i.distance(raw_j) - r_i - r_j
                need = max(clr_i, clr_j)
                if gap < need - 1e-6:
                    violations.append(
                        {
                            "layer": layer,
                            "a": lab_i,
                            "b": lab_j,
                            "gap": round(gap, 3),
                            "need": round(need, 3),
                            "where": tuple(round(c, 1) for c in g_i.centroid.coords[0]),
                        }
                    )

    opens = []
    if connectivity:
        opens = check_connectivity(board, result)
    return violations, opens


def check_pairs(board, result):
    """Measure differential-pair coupling: for each pair, the length of the
    P trace that runs further than the pair pitch from any N copper.
    Returns [(p_name, n_name, coupled_pct, uncoupled_mil), ...]."""
    from shapely.ops import unary_union

    from .diffpair import GAP_SLACK, find_diff_pairs

    out = []
    for net_p, net_n, gap in find_diff_pairs(board):
        pitch = net_p.width + gap + GAP_SLACK
        p_lines = [LineString(t.coords) for t in result.traces if t.net == net_p.name]
        n_geoms = [LineString(t.coords) for t in result.traces if t.net == net_n.name]
        if not p_lines or not n_geoms:
            out.append((net_p.name, net_n.name, 0.0, 0.0))
            continue
        n_union = unary_union(n_geoms)
        total = coupled = 0.0
        step = 5.0
        for line in p_lines:
            n_samples = max(2, int(line.length / step) + 1)
            for i in range(n_samples):
                pt = line.interpolate(i / (n_samples - 1), normalized=True)
                total += step
                if pt.distance(n_union) <= pitch * 1.5:
                    coupled += step
        out.append(
            (net_p.name, net_n.name,
             100.0 * coupled / total if total else 0.0,
             total - coupled)
        )
    return out


def check_geometry(board, result, turn_deg: float = 30.0):
    """Style/soundness checks on the final geometry:
    - dangling: trace endpoints whose copper touches no other same-net
      copper (open trace ends going nowhere)
    - corners: turns sharper than `turn_deg` at interior vertices or at
      trace-to-trace junctions, outside pad/via copper (where a corner
      would actually be visible on the board)
    Returns (dangling, corners), lists of dicts."""
    import math

    traces_by_net = defaultdict(list)
    for t in result.traces:
        if len(t.coords) >= 2:
            traces_by_net[t.net].append(t)
    vias_by_net = defaultdict(list)
    for v in result.vias:
        vias_by_net[v.net].append(v)
    pads_by_net = defaultdict(list)
    for pad in board.pads.values():
        if pad.net:
            pads_by_net[pad.net].append(pad)

    dangling = []
    corners = []
    for net_name, traces in traces_by_net.items():
        pieces = []  # (layer or '*', geom, trace_index or -1)
        for i, t in enumerate(traces):
            pieces.append((t.layer, LineString(t.coords).buffer(t.width / 2), i))
        for v in vias_by_net[net_name]:
            pieces.append(("*", Point(v.x, v.y).buffer(v.diameter / 2), -1))
        for pad in pads_by_net[net_name]:
            if pad.padstack.is_through():
                g = pad.geometry_on(next(iter(pad.layers())))
                if g is not None:
                    pieces.append(("*", g, -1))
            else:
                for layer in pad.layers():
                    g = pad.geometry_on(layer)
                    if g is not None:
                        pieces.append((layer, g, -1))
        cover = [(ly, g) for ly, g, i in pieces if i == -1]

        def covered(e, layer):
            pt = Point(e)
            for ly, g in cover:
                if (ly == "*" or ly == layer) and g.covers(pt):
                    return True
            return False

        # dangling ends
        for i, t in enumerate(traces):
            for e in (t.coords[0], t.coords[-1]):
                pt = Point(e)
                ok = False
                for ly, g, j in pieces:
                    if j == i or (ly != "*" and ly != t.layer):
                        continue
                    if g.distance(pt) < t.width / 2 - 1e-3:
                        ok = True
                        break
                if not ok:
                    dangling.append(
                        {"net": net_name, "layer": t.layer,
                         "where": (round(e[0], 1), round(e[1], 1))}
                    )

        # interior sharp corners
        for t in traces:
            c = t.coords
            for k in range(1, len(c) - 1):
                a = (c[k][0] - c[k - 1][0], c[k][1] - c[k - 1][1])
                b = (c[k + 1][0] - c[k][0], c[k + 1][1] - c[k][1])
                la, lb = math.hypot(*a), math.hypot(*b)
                if la < 1e-6 or lb < 1e-6:
                    continue
                cosv = max(-1.0, min(1.0, (a[0] * b[0] + a[1] * b[1]) / (la * lb)))
                turn = math.degrees(math.acos(cosv))
                if turn > turn_deg and not covered(c[k], t.layer):
                    corners.append(
                        {"net": t.net, "layer": t.layer,
                         "where": (round(c[k][0], 1), round(c[k][1], 1)),
                         "turn": round(turn, 1)}
                    )
        # junction corners: two trace ends meeting end-to-end must continue
        # smoothly (chains split by width changes cannot be chain-merged)
        ends = defaultdict(list)
        for t in traces:
            c = t.coords
            for e, nb in ((c[0], c[1]), (c[-1], c[-2])):
                d = (nb[0] - e[0], nb[1] - e[1])
                length = math.hypot(*d)
                if length < 1e-6:
                    continue
                ends[(t.layer, round(e[0], 2), round(e[1], 2))].append(
                    (d[0] / length, d[1] / length)
                )
        for (layer, x, y), dirs in ends.items():
            if len(dirs) != 2:
                continue  # free end or branch point, not a continuation
            cosv = max(-1.0, min(1.0, dirs[0][0] * dirs[1][0] + dirs[0][1] * dirs[1][1]))
            turn = 180.0 - math.degrees(math.acos(cosv))
            if turn > turn_deg and not covered((x, y), layer):
                corners.append(
                    {"net": net_name, "layer": layer, "where": (x, y),
                     "turn": round(turn, 1)}
                )
    return dangling, corners


def check_connectivity(board, result):
    """Verify each net's pads form one connected component via its traces/vias."""
    from shapely.ops import unary_union

    opens = []
    traces_by_net = defaultdict(list)
    for t in result.traces:
        traces_by_net[t.net].append(t)
    vias_by_net = defaultdict(list)
    for v in result.vias:
        vias_by_net[v.net].append(v)

    for net in board.nets.values():
        pads = board.pads_of_net(net)
        if len(pads) < 2:
            continue
        # union copper per layer; vias and through-pad barrels join layers
        per_layer = defaultdict(list)
        for pad in pads:
            if pad.padstack.is_through():
                g = pad.geometry_on(next(iter(pad.layers())))
                if g is not None:
                    per_layer["*"].append((f"P{pad.pin_id}", g))
                continue
            for layer in pad.layers():
                g = pad.geometry_on(layer)
                if g is not None:
                    per_layer[layer].append((f"P{pad.pin_id}", g))
        for t in traces_by_net[net.name]:
            per_layer[t.layer].append(("T", LineString(t.coords).buffer(t.width / 2)))
        via_geoms = [
            ("V", Point(v.x, v.y).buffer(v.diameter / 2)) for v in vias_by_net[net.name]
        ]

        # union-find over all copper pieces
        pieces = []
        for layer, lst in per_layer.items():
            for label, g in lst:
                pieces.append((layer, label, g))
        for label, g in via_geoms:
            pieces.append(("*", label, g))  # '*' joins both layers

        parent = list(range(len(pieces)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            parent[find(i)] = find(j)

        for i in range(len(pieces)):
            li, _, gi = pieces[i]
            for j in range(i + 1, len(pieces)):
                lj, _, gj = pieces[j]
                if li != lj and li != "*" and lj != "*":
                    continue
                if gi.intersects(gj):
                    union(i, j)

        pad_roots = {
            find(i) for i, (_, label, _) in enumerate(pieces) if label.startswith("P")
        }
        if len(pad_roots) > 1:
            opens.append((net.name, len(pad_roots)))
    return opens
