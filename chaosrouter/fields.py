"""GIL-free field computations (numba): exact Euclidean distance transform
and disk erosion. Replaces scipy.ndimage so per-net routing pipelines can run
concurrently on multiple cores.
"""

from __future__ import annotations

import numpy as np
from numba import njit

INF = 1e12


@njit(cache=True, nogil=True)
def _edt_sq(obstacle):
    """Squared distance (in cells) from every cell to the nearest True cell.
    Felzenszwalb & Huttenlocher separable exact transform."""
    ny, nx = obstacle.shape
    d = np.empty((ny, nx), dtype=np.float64)

    # pass 1: 1D distances along columns
    for x in range(nx):
        prev = -1
        for y in range(ny):
            if obstacle[y, x]:
                prev = y
            d[y, x] = np.float64((y - prev) * (y - prev)) if prev >= 0 else INF
        nxt = -1
        for y in range(ny - 1, -1, -1):
            if obstacle[y, x]:
                nxt = y
            if nxt >= 0:
                v = np.float64((nxt - y) * (nxt - y))
                if v < d[y, x]:
                    d[y, x] = v

    # pass 2: lower envelope of parabolas along rows
    out = np.empty((ny, nx), dtype=np.float32)
    v = np.empty(nx, dtype=np.int64)
    z = np.empty(nx + 1, dtype=np.float64)
    for y in range(ny):
        k = 0
        v[0] = 0
        z[0] = -INF
        z[1] = INF
        for q in range(1, nx):
            fq = d[y, q] + q * q
            while True:
                p = v[k]
                s = (fq - (d[y, p] + p * p)) / (2.0 * q - 2.0 * p)
                if s <= z[k]:
                    k -= 1
                    if k < 0:
                        break
                else:
                    break
            k += 1
            v[k] = q
            z[k] = s
            z[k + 1] = INF
        k = 0
        for q in range(nx):
            while z[k + 1] < q:
                k += 1
            p = v[k]
            out[y, q] = np.float32((q - p) * (q - p) + d[y, p])
    return out


@njit(cache=True, nogil=True)
def distance_field(obstacle, step):
    """Distance in mils from each cell to the nearest True (obstacle) cell."""
    return np.sqrt(_edt_sq(obstacle)) * step


@njit(cache=True, nogil=True)
def erode_disk(mask, r_cells):
    """Binary erosion by a disk: keep cells whose nearest False cell is
    strictly farther than r_cells (identical to scipy disk erosion)."""
    sq = _edt_sq(~mask)
    out = np.empty(mask.shape, dtype=np.bool_)
    r2 = np.float32(r_cells * r_cells)
    ny, nx = mask.shape
    for y in range(ny):
        for x in range(nx):
            out[y, x] = sq[y, x] > r2
    return out


@njit(cache=True, nogil=True)
def seg_clear(px, py, qx, qy, d_layer, own_layer, req, x0, y0, step, nx, ny):
    """True if the segment p->q stays clear (distance-field >= req, or own
    copper) at every sample. Fully nogil so callers parallelize."""
    dx = qx - px
    dy = qy - py
    length = (dx * dx + dy * dy) ** 0.5
    n = max(2, int(length / (step * 0.5)) + 1)
    for k in range(n):
        t = k / (n - 1)
        x = px + dx * t
        y = py + dy * t
        ix = int(round((x - x0) / step))
        iy = int(round((y - y0) / step))
        if ix < 0:
            ix = 0
        elif ix >= nx:
            ix = nx - 1
        if iy < 0:
            iy = 0
        elif iy >= ny:
            iy = ny - 1
        if d_layer[iy, ix] < req and not own_layer[iy, ix]:
            return False
    return True


@njit(cache=True, nogil=True)
def string_pull(xs, ys, d_layer, own_layer, req, x0, y0, step, nx, ny):
    """Greedy line-of-sight shortcutting, entirely nogil. Returns the kept
    point indices (into xs/ys)."""
    m = len(xs)
    out = np.empty(m, dtype=np.int64)
    out[0] = 0
    o = 1
    i = 0
    while i < m - 1:
        j = m - 1
        while j > i + 1:
            if seg_clear(xs[i], ys[i], xs[j], ys[j], d_layer, own_layer,
                         req, x0, y0, step, nx, ny):
                break
            j -= 1
        out[o] = j
        o += 1
        i = j
    return out[:o]
