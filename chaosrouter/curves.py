"""Curve engine: replace polyline corners with tangent arcs (fillets).

Every arc is clearance-checked via a caller-supplied predicate; on violation
the radius is halved (twice) before giving up and keeping the sharp corner.
"""

from __future__ import annotations

import math

import numpy as np

CHORD = 2.5  # mil, arc sampling chord length
MIN_TURN = math.radians(0.5)  # below this a turn is numerically straight


def _arc_points(p1, corner, p2, r, turn_sign):
    """Sample a tangent arc replacing `corner`; p1/p2 are the tangent points."""
    ux = (corner[0] - p1[0], corner[1] - p1[1])
    lu = math.hypot(*ux)
    ux = (ux[0] / lu, ux[1] / lu)
    # normal pointing to arc center (left of direction if turning left)
    n = (-ux[1] * turn_sign, ux[0] * turn_sign)
    cx, cy = p1[0] + n[0] * r, p1[1] + n[1] * r
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    a2 = math.atan2(p2[1] - cy, p2[0] - cx)
    sweep = a2 - a1
    # normalize sweep to the correct rotation direction
    if turn_sign > 0 and sweep < 0:
        sweep += 2 * math.pi
    elif turn_sign < 0 and sweep > 0:
        sweep -= 2 * math.pi
    n_seg = max(2, int(abs(sweep) * r / CHORD) + 1)
    return [
        (cx + r * math.cos(a1 + sweep * t), cy + r * math.sin(a1 + sweep * t))
        for t in np.linspace(0.0, 1.0, n_seg + 1)
    ]


def fillet_polyline(pts, r_target: float, is_clear) -> list:
    """Round every corner of pts with radius <= r_target.

    is_clear(arc_points) -> bool checks copper clearance for a candidate arc.
    Endpoints are preserved exactly.
    """
    if len(pts) < 3 or r_target <= 0:
        return list(pts)

    pts = [tuple(p) for p in pts]
    seg_budget = []  # how much length each segment can donate per end
    for a, b in zip(pts[:-1], pts[1:]):
        seg_budget.append(math.hypot(b[0] - a[0], b[1] - a[1]) * 0.5 - 1e-6)

    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        A, B, C = pts[i - 1], pts[i], pts[i + 1]
        u = (B[0] - A[0], B[1] - A[1])
        v = (C[0] - B[0], C[1] - B[1])
        lu, lv = math.hypot(*u), math.hypot(*v)
        if lu < 1e-9 or lv < 1e-9:
            continue
        u = (u[0] / lu, u[1] / lu)
        v = (v[0] / lv, v[1] / lv)
        cross = u[0] * v[1] - u[1] * v[0]
        dot = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
        turn = math.atan2(abs(cross), dot)  # 0..pi
        if turn < MIN_TURN or turn > math.pi - 1e-3:
            out.append(B)
            continue
        turn_sign = 1.0 if cross > 0 else -1.0
        t_max = min(seg_budget[i - 1], seg_budget[i])

        placed = False
        r = r_target
        while r >= 0.4:
            t = r * math.tan(turn / 2)
            if t > t_max:
                t = t_max
                r = t / math.tan(turn / 2)
                if r < 0.4:
                    break
            p1 = (B[0] - u[0] * t, B[1] - u[1] * t)
            p2 = (B[0] + v[0] * t, B[1] + v[1] * t)
            arc = _arc_points(p1, B, p2, r, turn_sign)
            if is_clear(arc):
                out.extend(arc)
                placed = True
                break
            r *= 0.5
        if not placed:
            out.append(B)
    out.append(pts[-1])

    # drop consecutive duplicates
    dedup = [out[0]]
    for p in out[1:]:
        if math.hypot(p[0] - dedup[-1][0], p[1] - dedup[-1][1]) > 1e-6:
            dedup.append(p)
    return dedup


def _merge_chains(result):
    """Merge same-net/layer/width traces that meet end-to-end, so the joint
    becomes an interior vertex the fillet pass can round."""
    from collections import defaultdict

    def q(p):
        return (round(p[0], 2), round(p[1], 2))

    groups = defaultdict(list)
    keep = []
    for t in result.traces:
        if t.no_fillet:
            keep.append(t)
        else:
            groups[(t.net, t.layer, round(t.width, 4))].append(t)

    for ts in groups.values():
        changed = True
        while changed and len(ts) > 1:
            changed = False
            endmap = defaultdict(list)
            for i, t in enumerate(ts):
                endmap[q(t.coords[0])].append(i)
                endmap[q(t.coords[-1])].append(i)
            for pt, idxs in endmap.items():
                if len(idxs) != 2 or idxs[0] == idxs[1]:
                    continue
                a, b = ts[idxs[0]], ts[idxs[1]]
                ca, cb = list(a.coords), list(b.coords)
                if q(ca[0]) == pt:
                    ca.reverse()
                if q(cb[-1]) == pt:
                    cb.reverse()
                if q(ca[-1]) != pt or q(cb[0]) != pt:
                    continue
                a.coords = ca + cb[1:]
                ts.pop(idxs[1] if idxs[1] > idxs[0] else idxs[0])
                changed = True
                break
        keep.extend(ts)
    result.traces = keep


def fillet_result(result, ws, board, r_target: float = 12.0):
    """Fillet every trace in a RouteResult, exact-checking each arc.
    Coupled diff-pair sections are already curved and are left alone."""
    _merge_chains(result)
    for t in result.traces:
        if t.no_fillet:
            continue
        net = board.nets[t.net]

        def is_clear(arc_pts, _t=t, _net=net):
            return ws.exact_trace_ok(_t.net, _t.layer, arc_pts, _t.width, _net.clearance)

        t.coords = fillet_polyline(t.coords, r_target, is_clear)
    return result
