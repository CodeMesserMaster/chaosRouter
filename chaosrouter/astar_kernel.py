"""Numba-compiled A* kernel - same algorithm as the Python loop, ~50x faster.

State = layer * (wy*wx) + iy*wx + ix. 8-connected moves plus via moves that
jump between all layer pairs. Returns (found_state, parent array).
"""

from __future__ import annotations

import numpy as np
from numba import njit

SQRT2 = np.sqrt(2.0)


@njit(cache=True, nogil=True)
def _heap_push(keys, vals, size, key, val):
    i = size
    keys[i] = key
    vals[i] = val
    while i > 0:
        p = (i - 1) >> 1
        if keys[p] <= keys[i]:
            break
        keys[p], keys[i] = keys[i], keys[p]
        vals[p], vals[i] = vals[i], vals[p]
        i = p
    return size + 1


@njit(cache=True, nogil=True)
def _sift_down(keys, vals, size, i):
    while True:
        l = 2 * i + 1
        r = l + 1
        smallest = i
        if l < size and keys[l] < keys[smallest]:
            smallest = l
        if r < size and keys[r] < keys[smallest]:
            smallest = r
        if smallest == i:
            break
        keys[smallest], keys[i] = keys[i], keys[smallest]
        vals[smallest], vals[i] = vals[i], vals[smallest]
        i = smallest


@njit(cache=True, nogil=True)
def _compact(keys, vals, size, gcost):
    """Drop stale heap entries (superseded by a better gcost), re-heapify."""
    j = 0
    for i in range(size):
        if keys[i] <= gcost[vals[i]] + 1e-6:
            keys[j] = keys[i]
            vals[j] = vals[i]
            j += 1
    for i in range(j // 2 - 1, -1, -1):
        _sift_down(keys, vals, j, i)
    return j


@njit(cache=True, nogil=True)
def _heap_pop(keys, vals, size):
    top_key = keys[0]
    top_val = vals[0]
    size -= 1
    keys[0] = keys[size]
    vals[0] = vals[size]
    i = 0
    while True:
        l = 2 * i + 1
        r = l + 1
        smallest = i
        if l < size and keys[l] < keys[smallest]:
            smallest = l
        if r < size and keys[r] < keys[smallest]:
            smallest = r
        if smallest == i:
            break
        keys[smallest], keys[i] = keys[i], keys[smallest]
        vals[smallest], vals[i] = vals[i], vals[smallest]
        i = smallest
    return top_key, top_val, size


@njit(cache=True, nogil=True)
def dijkstra_cost(
    trav,     # bool[nl*wy*wx] passable (already includes crossable overlay)
    goal,     # bool[nl*wy*wx]
    cost,     # float32[nl*wy*wx] extra cost for entering each cell
    via_ok,   # bool[wy*wx] layer changes only where a via can physically fit
    starts,   # int64[n]
    nl, wy, wx,
    step, via_cost,
):
    """Relaxed reachability search for rip-up diagnosis. Crossing foreign
    traces costs `cost`; layer changes need a feasible via site, so the
    diagnosis never hallucinates impossible escapes."""
    n_states = nl * wy * wx
    stride = wy * wx
    gcost = np.full(n_states, np.inf, dtype=np.float64)
    parent = np.full(n_states, -1, dtype=np.int64)

    cap = 4 * n_states + 64
    keys = np.empty(cap, dtype=np.float64)
    vals = np.empty(cap, dtype=np.int64)
    size = 0
    diag = step * SQRT2

    for k in range(starts.shape[0]):
        s = starts[k]
        if trav[s] and gcost[s] > 0.0:
            gcost[s] = 0.0
            size = _heap_push(keys, vals, size, 0.0, s)

    found = np.int64(-1)
    while size > 0:
        f, s, size = _heap_pop(keys, vals, size)
        if f > gcost[s] + 1e-9:
            continue
        if goal[s]:
            found = s
            break
        li = s // stride
        rem = s % stride
        iy = rem // wx
        ix = rem % wx
        g = gcost[s]
        for m in range(8):
            if m == 0:
                dx, dy, c = 1, 0, step
            elif m == 1:
                dx, dy, c = -1, 0, step
            elif m == 2:
                dx, dy, c = 0, 1, step
            elif m == 3:
                dx, dy, c = 0, -1, step
            elif m == 4:
                dx, dy, c = 1, 1, diag
            elif m == 5:
                dx, dy, c = 1, -1, diag
            elif m == 6:
                dx, dy, c = -1, 1, diag
            else:
                dx, dy, c = -1, -1, diag
            nx = ix + dx
            ny = iy + dy
            if nx < 0 or ny < 0 or nx >= wx or ny >= wy:
                continue
            ns = s + dy * wx + dx
            if not trav[ns]:
                continue
            ng = g + c + cost[ns]
            if ng < gcost[ns] - 1e-9:
                gcost[ns] = ng
                parent[ns] = s
                if size >= cap - 1:
                    size = _compact(keys, vals, size, gcost)
                if size < cap - 1:
                    size = _heap_push(keys, vals, size, ng, ns)
        if via_ok[rem]:
            for lj in range(nl):
                if lj == li:
                    continue
                ns = lj * stride + rem
                if not trav[ns]:
                    continue
                ng = g + via_cost + cost[ns]
                if ng < gcost[ns] - 1e-9:
                    gcost[ns] = ng
                    parent[ns] = s
                    if size >= cap - 1:
                        size = _compact(keys, vals, size, gcost)
                    if size < cap - 1:
                        size = _heap_push(keys, vals, size, ng, ns)

    return found, parent


@njit(cache=True, nogil=True)
def astar(
    trav,      # bool[nl*wy*wx] traversable
    goal,      # bool[nl*wy*wx]
    via_ok,    # bool[wy*wx]
    cong,      # float32[nl*wy*wx] congestion cost for entering each cell
    starts,    # int64[n] start states (g=0)
    nl, wy, wx,
    step, via_cost,
    g_ix0, g_ix1, g_iy0, g_iy1,  # goal bbox for the octile heuristic
):
    n_states = nl * wy * wx
    stride = wy * wx
    gcost = np.full(n_states, np.inf, dtype=np.float64)
    parent = np.full(n_states, -1, dtype=np.int64)

    cap = 4 * n_states + 64
    keys = np.empty(cap, dtype=np.float64)
    vals = np.empty(cap, dtype=np.int64)
    size = 0

    diag = step * SQRT2
    coef = SQRT2 - 1.0

    for k in range(starts.shape[0]):
        s = starts[k]
        if trav[s] and gcost[s] > 0.0:
            gcost[s] = 0.0
            rem = s % stride
            iy = rem // wx
            ix = rem % wx
            dx = max(0, max(g_ix0 - ix, ix - g_ix1))
            dy = max(0, max(g_iy0 - iy, iy - g_iy1))
            h = (max(dx, dy) + coef * min(dx, dy)) * step
            size = _heap_push(keys, vals, size, h, s)

    found = np.int64(-1)
    while size > 0:
        f, s, size = _heap_pop(keys, vals, size)
        li = s // stride
        rem = s % stride
        iy = rem // wx
        ix = rem % wx
        g = gcost[s]
        dxh = max(0, max(g_ix0 - ix, ix - g_ix1))
        dyh = max(0, max(g_iy0 - iy, iy - g_iy1))
        h = (max(dxh, dyh) + coef * min(dxh, dyh)) * step
        if f > g + h + 1e-9:
            continue
        if goal[s]:
            found = s
            break
        for m in range(8):
            if m == 0:
                dx, dy, cost = 1, 0, step
            elif m == 1:
                dx, dy, cost = -1, 0, step
            elif m == 2:
                dx, dy, cost = 0, 1, step
            elif m == 3:
                dx, dy, cost = 0, -1, step
            elif m == 4:
                dx, dy, cost = 1, 1, diag
            elif m == 5:
                dx, dy, cost = 1, -1, diag
            elif m == 6:
                dx, dy, cost = -1, 1, diag
            else:
                dx, dy, cost = -1, -1, diag
            nx = ix + dx
            ny = iy + dy
            if nx < 0 or ny < 0 or nx >= wx or ny >= wy:
                continue
            ns = s + dy * wx + dx
            if goal[ns]:
                # goals accepted on ARRIVAL: target copper may lie inside
                # clearance fields and need not be traversable — the exact
                # geometry validation remains the judge
                parent[ns] = s
                found = ns
                break
            if not trav[ns]:
                continue
            ng = g + cost + cong[ns]
            if ng < gcost[ns] - 1e-9:
                gcost[ns] = ng
                parent[ns] = s
                hx = max(0, max(g_ix0 - nx, nx - g_ix1))
                hy = max(0, max(g_iy0 - ny, ny - g_iy1))
                nh = (max(hx, hy) + coef * min(hx, hy)) * step
                if size >= cap - 1:
                    size = _compact(keys, vals, size, gcost)
                if size < cap - 1:
                    size = _heap_push(keys, vals, size, ng + nh, ns)
        if found >= 0:
            break
        if via_ok[rem]:
            for lj in range(nl):
                if lj == li:
                    continue
                ns = lj * stride + rem
                if goal[ns]:
                    parent[ns] = s
                    found = ns
                    break
                if not trav[ns]:
                    continue
                ng = g + via_cost + cong[ns]
                if ng < gcost[ns] - 1e-9:
                    gcost[ns] = ng
                    parent[ns] = s
                    hx = max(0, max(g_ix0 - ix, ix - g_ix1))
                    hy = max(0, max(g_iy0 - iy, iy - g_iy1))
                    nh = (max(hx, hy) + coef * min(hx, hy)) * step
                    if size >= cap - 1:
                        size = _compact(keys, vals, size, gcost)
                    if size < cap - 1:
                        size = _heap_push(keys, vals, size, ng + nh, ns)
            if found >= 0:
                break

    return found, parent
