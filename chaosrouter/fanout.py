"""Planned fanout — coordinated escape routing for fine-pitch ICs.

The router routes nets independently, so on a dense fine-pitch part each net
tries to escape the pad field AND reach its destination in one shot; adjacent
escapes fight over the ~50mil strip just off the pads and later ones get walled
(see routing-methodology memory: the escape, not the room, is the wall).

planned_fanout() fixes that by escaping the WHOLE part first, as an ordered
parallel bundle: each pin gets a short straight escape out of the field to an
aligned breakout, routed in pin-position order so the lanes never cross. The
breakout becomes the net's routing terminal (router._escape), so the later
Manhattan/route phase connects from OUTSIDE the field where there's room.

Escapes neck down (thin trace + reduced clearance, via router.neck_gap) so they
fit the tight pitch — the same rule that lets a fat power net leave a fine pad.
"""

from __future__ import annotations

import math
from collections import defaultdict


def _min_pitch(pads) -> float:
    m = 1e9
    for i in range(len(pads)):
        for j in range(i + 1, len(pads)):
            d = math.hypot(pads[i].x - pads[j].x, pads[i].y - pads[j].y)
            if d < m:
                m = d
    return m


def dense_ics(board, min_pins: int = 12, max_pitch: float = 40.0):
    """Refs of fine-pitch parts worth planning an escape bundle for."""
    by_ref = defaultdict(list)
    for p in board.pads.values():
        by_ref[p.ref].append(p)
    out = []
    for ref, pads in by_ref.items():
        if len(pads) >= min_pins and _min_pitch(pads) < max_pitch:
            out.append((ref, pads))
    return out


def planned_fanout(router, escape_gap: float = 45.0, progress=None,
                   skip_nets=frozenset()) -> int:
    """Route a coordinated escape bundle for every fine-pitch IC. Returns the
    number of pins escaped. Sets router._escape[pin_id] = (bx, by, layer) so
    the main routing phase starts each escaped pin from its breakout. skip_nets
    (plane + diff-pair nets) are left alone — planes drop straight to a via and
    diff pairs route coupled, neither wants a single-ended fanout stub."""
    from .router import Trace

    b = router.board
    ws = router.ws
    if getattr(router, "_escape", None) is None:
        router._escape = {}
    planes = getattr(b, "plane_nets", frozenset())
    skip = set(planes) | set(skip_nets)

    n_esc = 0
    for ref, pads in dense_ics(b):
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        cx, cy = sum(xs) / len(pads), sum(ys) / len(pads)
        hw, hh = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
        routable = [p for p in pads if p.net and p.net not in skip]

        # order: by escape side, then position ALONG the row — so adjacent
        # pins escape into adjacent lanes in sequence (the bundle can't cross)
        def order_key(p):
            ox, oy = p.x - cx, p.y - cy
            horiz = abs(ox) >= abs(oy)
            side = (0 if horiz else 1, 1 if (ox if horiz else oy) >= 0 else -1)
            pos = p.y if horiz else p.x
            return (side, pos)

        for p in sorted(routable, key=order_key):
            net = b.nets.get(p.net)
            if net is None:
                continue
            layer = next(iter(p.layers()))
            ox, oy = p.x - cx, p.y - cy
            horiz = abs(ox) >= abs(oy)
            # neck the escape: thin + reduced clearance so it fits the pitch
            w = router.neck_width(net)
            clr = router.neck_gap(net)
            # try the straight escape first, then progressively further out and
            # a small lateral fan — a pin whose direct lane is taken can still
            # reach a breakout a bit deeper or offset, which is what makes the
            # bundle CLEAR EVERY pin instead of most of them
            placed = False
            for extra in (0.0, 25.0, 55.0, 90.0):
                gap = escape_gap + extra
                if horiz:
                    d = 1 if ox >= 0 else -1
                    cands = [(cx + d * (hw + gap), p.y),
                             (cx + d * (hw + gap), p.y + 18),
                             (cx + d * (hw + gap), p.y - 18)]
                else:
                    d = 1 if oy >= 0 else -1
                    cands = [(p.x, cy + d * (hh + gap)),
                             (p.x + 18, cy + d * (hh + gap)),
                             (p.x - 18, cy + d * (hh + gap))]
                for bx, by in cands:
                    coords = [(p.x, p.y), (bx, by)]
                    if not ws.exact_trace_ok(p.net, layer, coords, w, clr):
                        continue
                    with router._result_lock:
                        router.result.traces.append(
                            Trace(p.net, layer, coords, w, is_escape=True)
                        )
                    ws.add_trace(p.net, layer, coords, w, kind="escape")
                    router._escape[p.pin_id] = (bx, by, layer)
                    n_esc += 1
                    placed = True
                    break
                if placed:
                    break
        if progress:
            progress(0, 0, f"fanout {ref}: escaped", router.result)
    return n_esc
