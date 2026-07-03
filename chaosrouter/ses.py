"""Specctra SES session file writer (for re-import into DipTrace)."""

from __future__ import annotations

import os
from collections import defaultdict

# SES convention (measured against DipTrace's importer):
# coordinates: integers in 1/1000 mil with (resolution mil 1000) — verified
# correct positions on import. Width/via-size values may use a different
# divisor in DipTrace's reader; see _dim() below.
RES_UNIT = "mil"
RES = 1000  # increments per mil


def _i(v_mil: float) -> int:
    return int(round(v_mil * RES))


# dimension values (trace width, via diameter): DipTrace imported these too
# big while coordinates were right; _DIM_SCALE is the empirically correct
# divisor relative to coordinates (tune: 1000 = same as coordinates).
DIM_RES = 1000


def _dim(v_mil: float) -> int:
    return int(round(v_mil * DIM_RES))


def _q(name: str) -> str:
    return f'"{name}"' if any(c in name for c in ' ()"') else name


def _unify_net_wires(traces, pads, vias=()):
    """Make connectivity explicit for CAD importers (DipTrace unites wires
    only at coincident endpoints / pads / vias, not mid-trace):
    - a wire endpoint landing on another wire's body becomes a real
      junction: the endpoint snaps onto the host centerline and the host
      wire is split there, so three wires meet at one exact coordinate;
    - a wire endpoint landing inside an own-net pad gets the pad snap
      point appended so the CAD sees the pad connection."""
    from shapely.geometry import LineString, Point

    wires = [
        {"layer": layer, "coords": [tuple(c) for c in coords], "width": width}
        for layer, coords, width in traces
    ]

    def q(p):
        return (round(p[0], 3), round(p[1], 3))

    # via snap: endpoint on a via barrel but not at its center -> extend
    for w in wires:
        for end_i in (0, -1):
            e = w["coords"][end_i]
            for vx, vy, vd in vias:
                dx, dy = vx - e[0], vy - e[1]
                if dx * dx + dy * dy <= (vd / 2) ** 2 and (
                    abs(dx) > 1e-3 or abs(dy) > 1e-3
                ):
                    if end_i == 0:
                        w["coords"].insert(0, (vx, vy))
                    else:
                        w["coords"].append((vx, vy))
                    break

    # pad snap: endpoint inside a pad but not at its origin -> extend
    for w in wires:
        for end_i in (0, -1):
            e = w["coords"][end_i]
            for pad in pads:
                if abs(pad.x - e[0]) > 60 or abs(pad.y - e[1]) > 60:
                    continue
                g = pad.geometry_on(w["layer"])
                if g is None:
                    continue
                if g.covers(Point(e)) and (abs(pad.x - e[0]) > 1e-3
                                           or abs(pad.y - e[1]) > 1e-3):
                    tail = (pad.x, pad.y)
                    if end_i == 0:
                        w["coords"].insert(0, tail)
                    else:
                        w["coords"].append(tail)
                    break

    # endpoint index
    endpoints = {}
    for i, w in enumerate(wires):
        endpoints.setdefault(q(w["coords"][0]), set()).add(i)
        endpoints.setdefault(q(w["coords"][-1]), set()).add(i)

    # find T junctions: endpoint on another wire's body
    splits: dict[int, list] = {}
    for i, w in enumerate(wires):
        for end_i in (0, -1):
            e = w["coords"][end_i]
            if len(endpoints.get(q(e), ())) > 1:
                continue  # already an explicit junction
            for j, host in enumerate(wires):
                if j == i or host["layer"] != w["layer"]:
                    continue
                if len(host["coords"]) < 2:
                    continue
                line = LineString(host["coords"])
                if line.distance(Point(e)) > (host["width"] + w["width"]) / 2:
                    continue
                s = line.project(Point(e))
                near = (host["width"] + w["width"]) / 2
                if s < near or s > line.length - near:
                    # lands beside a host END: snap to that endpoint exactly
                    tip = (host["coords"][0] if s < near
                           else host["coords"][-1])
                    if end_i == 0:
                        w["coords"][0] = tip
                    else:
                        w["coords"][-1] = tip
                    break
                p = line.interpolate(s)
                # snap the branch tip onto the host centerline
                if end_i == 0:
                    w["coords"][0] = (p.x, p.y)
                else:
                    w["coords"][-1] = (p.x, p.y)
                splits.setdefault(j, []).append(s)
                break

    # split hosts at junction points
    out = []
    for j, w in enumerate(wires):
        cuts = sorted(set(splits.get(j, [])))
        if not cuts:
            out.append((w["layer"], w["coords"], w["width"]))
            continue
        line = LineString(w["coords"])
        stations = [0.0] + cuts + [line.length]
        for a, b in zip(stations, stations[1:]):
            if b - a < 1e-6:
                continue
            seg_pts = [line.interpolate(a)]
            for k, c in enumerate(w["coords"]):
                s = line.project(Point(c))
                if a + 1e-9 < s < b - 1e-9:
                    seg_pts.append(Point(c))
            seg_pts.append(line.interpolate(b))
            out.append(
                (w["layer"], [(p.x, p.y) for p in seg_pts], w["width"])
            )
    return out


def write_ses(path: str, dsn_path: str, board, result, via_map=None):
    via_map = via_map or {}
    design = os.path.basename(dsn_path)
    lines = []
    a = lines.append
    a(f"(session {_q(os.path.splitext(design)[0] + '.ses')}")
    a(f"  (base_design {_q(design)})")
    a("  (placement")
    a(f"    (resolution {RES_UNIT} {RES})")
    comps = defaultdict(list)
    for c in board.components.values():
        comps[c.image_name].append(c)
    for image_name, cs in comps.items():
        a(f"    (component {_q(image_name)}")
        for c in cs:
            rot = int(c.rotation) if c.rotation == int(c.rotation) else c.rotation
            a(f"      (place {_q(c.ref)} {_i(c.x)} {_i(c.y)} {c.side} {rot})")
        a("    )")
    a("  )")
    a("  (was_is")
    a("  )")
    a("  (routes")
    a(f"    (resolution {RES_UNIT} {RES})")
    a("    (parser")
    a('      (string_quote ")')
    a("      (space_in_quoted_tokens on)")
    a("      (host_cad \"chaosRouter\")")
    a("      (host_version 0.1)")
    a("    )")

    # Via padstacks: only REDEFINE ones the CAD doesn't already know. Every
    # padstack that came from the input DSN is native to the CAD — emitting
    # our own (circle ...) shape for it overrides its true size on import
    # (this is why "inPadVia" vias imported oversized in DipTrace). We
    # reference those by name and let the CAD use its own definition.
    used_ps = sorted({via_map.get(v.padstack, v.padstack) for v in result.vias})
    unknown = [n for n in used_ps if n not in board.padstacks]
    if unknown:
        a("    (library_out")
        for ps_name in unknown:
            ps = next(
                (board.padstacks[k] for k, v in via_map.items()
                 if v == ps_name and k in board.padstacks), None)
            a(f"      (padstack {_q(ps_name)}")
            if ps:
                for sh in ps.shapes:
                    b = sh.geometry.bounds
                    dia = max(b[2] - b[0], b[3] - b[1])
                    a("        (shape")
                    a(f"          (circle {_q(sh.layer)} {_dim(dia)} 0 0)")
                    a("        )")
            a("        (attach off)")
            a("      )")
        a("    )")

    a("    (network_out")
    by_net: dict[str, dict] = defaultdict(lambda: {"traces": [], "vias": []})
    for t in result.traces:
        by_net[t.net]["traces"].append(t)
    for v in result.vias:
        by_net[v.net]["vias"].append(v)
    pads_by_net = defaultdict(list)
    for p in board.pads.values():
        if p.net:
            pads_by_net[p.net].append(p)
    for net_name, data in by_net.items():
        a(f"      (net {_q(net_name)}")
        wires = _unify_net_wires(
            [(t.layer, t.coords, t.width) for t in data["traces"]],
            pads_by_net.get(net_name, []),
            [(v.x, v.y, v.diameter) for v in data["vias"]],
        )
        for layer, coords, width in wires:
            coord_str = "\n            ".join(
                f"{_i(x)} {_i(y)}" for x, y in coords
            )
            a("        (wire")
            a(f"          (path {_q(layer)} {_dim(width)}")
            a(f"            {coord_str}")
            a("          )")
            a("        )")
        for v in data["vias"]:
            ps_out = via_map.get(v.padstack, v.padstack)
            a(f"        (via {_q(ps_out)} {_i(v.x)} {_i(v.y)})")
        a("      )")
    a("    )")
    a("  )")
    a(")")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
