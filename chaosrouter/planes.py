"""Plane connection — connect each plane-net pin to its plane.

Power/ground nets declared as planes (full copper layers) aren't routed pin-to-
pin; each pin is connected to the plane with a short stub + a via down to the
plane layer (a via anywhere on the solid plane layer joins the pour). A through-
hole pin already touches every layer, so it needs nothing. This runs before the
signal routing (for every router method) so the plane vias are reserved and the
signals weave around them — which is what frees the board.
"""

from __future__ import annotations

import math


def connect_to_planes(router, progress=None) -> int:
    """Drop a via (via a short stub when needed) from every off-plane pin to
    its plane layer. Returns the number of pins connected."""
    from .router import Trace, Via

    b = router.board
    ws = router.ws
    plane_of = getattr(b, "plane_layer_of", {})
    if not plane_of:
        return 0
    lock = getattr(router, "_result_lock", None)
    connected = 0

    for net_name in sorted(b.plane_nets):
        layers = plane_of.get(net_name)
        if not layers:
            continue  # region-only local pour (no full plane layer) — skip
        net = b.nets.get(net_name)
        if net is None:
            continue
        clr = net.net_class.clearance if net.net_class else b.default_clearance
        w = net.width
        # a real via padstack (never None) so the SES writer can name it —
        # a None padstack crashes write_ses when it sorts the padstack set
        vname, vdia = router.via_for(net)
        lset = set(layers)
        for p in b.pads_of_net(net):
            plys = list(p.layers())
            if lset.intersection(plys):
                continue  # through-hole / on-plane pin already joins the plane
            play = plys[0]
            # via-in-pad is forbidden — search outward for a clear via site,
            # nearest first, and stub the pin to it.
            done = False
            for dist in [12, 18, 24, 32, 40, 52, 64, 80]:
                for a in (0, 90, 180, 270, 45, 135, 225, 315):
                    bx = p.x + dist * math.cos(math.radians(a))
                    by = p.y + dist * math.sin(math.radians(a))
                    if not ws.exact_via_ok(net_name, bx, by, vdia, clr):
                        continue
                    stub = [(p.x, p.y), (bx, by)]
                    if not ws.exact_trace_ok(net_name, play, stub, w, clr):
                        continue
                    if lock:
                        with lock:
                            router.result.traces.append(Trace(net_name, play, stub, w))
                            router.result.vias.append(
                                Via(net_name, bx, by, vdia, padstack=vname)
                            )
                    else:
                        router.result.traces.append(Trace(net_name, play, stub, w))
                        router.result.vias.append(
                            Via(net_name, bx, by, vdia, padstack=vname)
                        )
                    ws.add_trace(net_name, play, stub, w)
                    ws.add_via(net_name, bx, by, vdia)
                    connected += 1
                    done = True
                    break
                if done:
                    break
        if progress:
            progress(0, 0, f"plane {net_name}: pins connected", router.result)
    return connected
