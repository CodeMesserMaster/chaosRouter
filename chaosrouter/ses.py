"""Specctra SES session file writer (for re-import into DipTrace)."""

from __future__ import annotations

import os
from collections import defaultdict

RES = 1000  # coordinate units per mil, matches (resolution mil 1000)


def _i(v: float) -> int:
    return int(round(v * RES))


def _q(name: str) -> str:
    return f'"{name}"' if any(c in name for c in ' ()"') else name


def write_ses(path: str, dsn_path: str, board, result):
    design = os.path.basename(dsn_path)
    lines = []
    a = lines.append
    a(f"(session {_q(os.path.splitext(design)[0] + '.ses')}")
    a(f"  (base_design {_q(design)})")
    a("  (placement")
    a(f"    (resolution mil {RES})")
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
    a(f"    (resolution mil {RES})")
    a("    (parser")
    a('      (string_quote ")')
    a("      (space_in_quoted_tokens on)")
    a("      (host_cad \"chaosRouter\")")
    a("      (host_version 0.1)")
    a("    )")

    # padstacks used by vias
    used_ps = sorted({v.padstack for v in result.vias})
    a("    (library_out")
    for ps_name in used_ps:
        ps = board.padstacks.get(ps_name)
        a(f"      (padstack {_q(ps_name)}")
        if ps:
            for sh in ps.shapes:
                b = sh.geometry.bounds
                dia = max(b[2] - b[0], b[3] - b[1])
                a(f"        (shape")
                a(f"          (circle {_q(sh.layer)} {_i(dia)} 0 0)")
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
    for net_name, data in by_net.items():
        a(f"      (net {_q(net_name)}")
        for t in data["traces"]:
            coord_str = "\n            ".join(
                f"{_i(x)} {_i(y)}" for x, y in t.coords
            )
            a("        (wire")
            a(f"          (path {_q(t.layer)} {_i(t.width)}")
            a(f"            {coord_str}")
            a("          )")
            a("        )")
        for v in data["vias"]:
            a(f"        (via {_q(v.padstack)} {_i(v.x)} {_i(v.y)})")
        a("      )")
    a("    )")
    a("  )")
    a(")")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
