"""Nogil numba geometry kernels — exact clearance math without shapely.

These replace the shapely calls in the exact-geometry validator so routing
releases the GIL (threads parallelize) and the hot path is compiled. Every
kernel is checked against shapely in tests/test_geom_kernels.py before it is
trusted — the router's whole correctness guarantee lives here.

Copper is represented as three flat "soups" a trace segment is tested
against:
  * segments  — trace/edge centerlines, each carrying a half-width
  * circles   — vias and (as a fallback) round pads, center + radius
  * polygons  — pad outlines, as edge lists

Distances are TRUE geometry (centerline distance minus radii), matching the
shapely `raw.distance(other) - r_a - r_b` convention exactly.
"""

from __future__ import annotations

import numpy as np
from numba import njit


@njit(cache=True, nogil=True, inline="always")
def _clampf(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


@njit(cache=True, nogil=True)
def seg_point_dist2(ax, ay, bx, by, px, py):
    """Squared distance from point (px,py) to segment AB."""
    dx = bx - ax
    dy = by - ay
    d2 = dx * dx + dy * dy
    if d2 < 1e-12:
        ex = px - ax
        ey = py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / d2
    t = _clampf(t, 0.0, 1.0)
    cx = ax + t * dx
    cy = ay + t * dy
    ex = px - cx
    ey = py - cy
    return ex * ex + ey * ey


@njit(cache=True, nogil=True)
def _orient(ax, ay, bx, by, cx, cy):
    return (by - ay) * (cx - bx) - (bx - ax) * (cy - by)


@njit(cache=True, nogil=True)
def _segs_intersect(ax, ay, bx, by, cx, cy, dx, dy):
    d1 = _orient(cx, cy, dx, dy, ax, ay)
    d2 = _orient(cx, cy, dx, dy, bx, by)
    d3 = _orient(ax, ay, bx, by, cx, cy)
    d4 = _orient(ax, ay, bx, by, dx, dy)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


@njit(cache=True, nogil=True)
def seg_seg_dist2(ax, ay, bx, by, cx, cy, dx, dy):
    """Squared minimum distance between segments AB and CD."""
    if _segs_intersect(ax, ay, bx, by, cx, cy, dx, dy):
        return 0.0
    d = seg_point_dist2(ax, ay, bx, by, cx, cy)
    v = seg_point_dist2(ax, ay, bx, by, dx, dy)
    if v < d:
        d = v
    v = seg_point_dist2(cx, cy, dx, dy, ax, ay)
    if v < d:
        d = v
    v = seg_point_dist2(cx, cy, dx, dy, bx, by)
    if v < d:
        d = v
    return d


@njit(cache=True, nogil=True)
def point_in_poly(px, py, poly_x, poly_y):
    """True if point is inside the polygon (ray casting)."""
    n = len(poly_x)
    inside = False
    j = n - 1
    for i in range(n):
        yi = poly_y[i]
        yj = poly_y[j]
        if (yi > py) != (yj > py):
            xint = poly_x[i] + (py - yi) / (yj - yi) * (poly_x[j] - poly_x[i])
            if px < xint:
                inside = not inside
        j = i
    return inside


@njit(cache=True, nogil=True)
def seg_poly_dist(ax, ay, bx, by, poly_x, poly_y):
    """Minimum distance from segment AB to a (filled) polygon. 0 if the
    segment touches or enters the polygon."""
    n = len(poly_x)
    # segment endpoint inside polygon -> distance 0
    if point_in_poly(ax, ay, poly_x, poly_y) or point_in_poly(bx, by, poly_x, poly_y):
        return 0.0
    best = 1e30
    j = n - 1
    for i in range(n):
        d2 = seg_seg_dist2(ax, ay, bx, by, poly_x[j], poly_y[j],
                           poly_x[i], poly_y[i])
        if d2 < best:
            best = d2
        j = i
    return best ** 0.5


@njit(cache=True, nogil=True)
def trace_violates(
    txs, tys, half, clr,
    seg_x0, seg_y0, seg_x1, seg_y1, seg_r, seg_clr,      # foreign segments
    cir_x, cir_y, cir_r, cir_clr,                         # foreign circles
    pverts_x, pverts_y, poff, pclr,                       # foreign polygons (CSR)
    eps,
):
    """True if the trace (polyline txs/tys, half-width `half`, clearance
    `clr`) comes closer than the required clearance to ANY foreign item.
    Gaps are true-geometry: centerline distance minus radii."""
    nt = len(txs) - 1
    # vs segments (other traces, board edge)
    for s in range(len(seg_x0)):
        need = clr if clr > seg_clr[s] else seg_clr[s]
        rr = half + seg_r[s]
        for i in range(nt):
            d2 = seg_seg_dist2(txs[i], tys[i], txs[i+1], tys[i+1],
                               seg_x0[s], seg_y0[s], seg_x1[s], seg_y1[s])
            if d2 ** 0.5 - rr < need + eps:
                return True
    # vs circles (vias, round pads)
    for c in range(len(cir_x)):
        need = clr if clr > cir_clr[c] else cir_clr[c]
        rr = half + cir_r[c]
        for i in range(nt):
            d2 = seg_point_dist2(txs[i], tys[i], txs[i+1], tys[i+1],
                                 cir_x[c], cir_y[c])
            if d2 ** 0.5 - rr < need + eps:
                return True
    # vs polygons (rect/poly pads)
    for p in range(len(poff) - 1):
        need = clr if clr > pclr[p] else pclr[p]
        a = poff[p]; b = poff[p + 1]
        px = pverts_x[a:b]; py = pverts_y[a:b]
        for i in range(nt):
            d = seg_poly_dist(txs[i], tys[i], txs[i+1], tys[i+1], px, py)
            if d - half < need + eps:
                return True
    return False


@njit(cache=True, nogil=True)
def _cell(x, y, x0, y0, inv_cell, nx):
    cx = int((x - x0) * inv_cell)
    cy = int((y - y0) * inv_cell)
    if cx < 0:
        cx = 0
    if cy < 0:
        cy = 0
    return cy * nx + cx


@njit(cache=True, nogil=True)
def trace_ok_csr(
    txs, tys, half, clr, own,
    # dynamic segments (traces): endpoints, half-width, clr, net
    sx0, sy0, sx1, sy1, sr, sclr, snet,
    # circles (vias): centre, radius, clr, net
    cx, cy, cr, cclr, cnet,
    # polygons (pads): CSR vertex store + per-poly clr/net
    pvx, pvy, poff, pclr, pnet,
    # spatial grids (CSR): seg / cir / poly cell -> item ids
    s_start, s_items, c_start, c_items, p_start, p_items,
    gx0, gy0, inv_cell, gnx, gny, ncells,
    eps,
):
    """True if the trace clears all FOREIGN copper. All candidate selection
    and distance math is here — no Python, fully nogil."""
    n = len(txs)
    # cells the trace's bbox touches (grown by reach)
    minx = txs[0]; maxx = txs[0]; miny = tys[0]; maxy = tys[0]
    for i in range(1, n):
        if txs[i] < minx:
            minx = txs[i]
        if txs[i] > maxx:
            maxx = txs[i]
        if tys[i] < miny:
            miny = tys[i]
        if tys[i] > maxy:
            maxy = tys[i]
    reach = half + clr + 3.0
    cxa = int((minx - reach - gx0) * inv_cell)
    cxb = int((maxx + reach - gx0) * inv_cell)
    cya = int((miny - reach - gy0) * inv_cell)
    cyb = int((maxy + reach - gy0) * inv_cell)
    if cxa < 0:
        cxa = 0
    if cya < 0:
        cya = 0
    if cxb >= gnx:
        cxb = gnx - 1
    if cyb >= gny:
        cyb = gny - 1
    nt = n - 1
    for cyy in range(cya, cyb + 1):
        for cxx in range(cxa, cxb + 1):
            cell = cyy * gnx + cxx
            # segments
            for t in range(s_start[cell], s_start[cell + 1]):
                s = s_items[t]
                if snet[s] == own:
                    continue
                need = clr if clr > sclr[s] else sclr[s]
                rr = half + sr[s]
                for i in range(nt):
                    d2 = seg_seg_dist2(txs[i], tys[i], txs[i+1], tys[i+1],
                                       sx0[s], sy0[s], sx1[s], sy1[s])
                    if d2 ** 0.5 - rr < need + eps:
                        return False
            # circles
            for t in range(c_start[cell], c_start[cell + 1]):
                s = c_items[t]
                if cnet[s] == own:
                    continue
                need = clr if clr > cclr[s] else cclr[s]
                rr = half + cr[s]
                for i in range(nt):
                    d2 = seg_point_dist2(txs[i], tys[i], txs[i+1], tys[i+1],
                                         cx[s], cy[s])
                    if d2 ** 0.5 - rr < need + eps:
                        return False
            # polygons
            for t in range(p_start[cell], p_start[cell + 1]):
                s = p_items[t]
                if pnet[s] == own:
                    continue
                need = clr if clr > pclr[s] else pclr[s]
                a = poff[s]; b = poff[s + 1]
                for i in range(nt):
                    d = seg_poly_dist(txs[i], tys[i], txs[i+1], tys[i+1],
                                      pvx[a:b], pvy[a:b])
                    if d - half < need + eps:
                        return False
    return True
