"""One-call routing pipeline shared by the CLI and the GUI.

run_pipeline() does everything route_board.py does — parse, route,
beautify, fillet, DRC, render, SES/PKL export — and returns a stats
dict the GUI can present.
"""

from __future__ import annotations

import math
import pickle
import time
from collections import defaultdict


def run_pipeline(
    dsn_path: str,
    out_base: str = "routed",
    step: float = 4.0,
    fillet_r: float = 25.0,
    sources: dict | None = None,
    drc: bool = True,
    progress=None,
) -> dict:
    """Route a DSN end to end. `progress(line: str)` receives log lines.
    Returns a dict of statistics and output paths."""

    def say(line: str):
        if progress:
            progress(line)

    from . import load_dsn
    from .curves import fillet_result
    from .drc import check, check_geometry, check_pairs
    from .grid import Workspace
    from .router import Router
    from .ses import write_ses
    from .viz import draw_board

    t0 = time.time()
    say(f"loading {dsn_path} ...")
    board = load_dsn(dsn_path)
    say(board.stats())

    ws = Workspace(board, step=step)
    router = Router(board, ws, power_sources=sources or {})

    def rp(i, n, name, res):
        if (isinstance(i, int) and n and (i % 10 == 0 or i == n)) or n == 0:
            say(
                f"[{i}/{n}] {name}  edges={res.routed_edges} "
                f"failed={len(res.failed)} vias={len(res.vias)}"
            )

    result = router.route_all(progress=rp)
    t_route = time.time() - t0
    say(
        f"routing done: {result.routed_edges} connections, "
        f"{len(result.failed)} failed, {len(result.vias)} vias ({t_route:.0f}s)"
    )

    pruned = router.prune_open_stubs()
    grafts = router.beautify_exits()
    fat = router.fatten_pad_entries()
    say(f"style: {pruned} stubs pruned, {grafts} exits grafted, {fat} entries fattened")
    say("filleting corners into arcs ...")
    fillet_result(result, ws, board, r_target=fillet_r)

    pairs = check_pairs(board, result)
    for p_name, n_name, pct, unc in pairs:
        say(f"pair {p_name}/{n_name}: {pct:.1f}% coupled, {unc:.0f} mil uncoupled")

    violations, opens, dangling, corners = [], [], [], []
    if drc:
        say("running DRC (exact geometry) ...")
        violations, opens = check(board, result)
        dangling, corners = check_geometry(board, result)
        say(
            f"DRC: {len(violations)} violations, {len(opens)} open nets, "
            f"{len(dangling)} dangling ends, {len(corners)} sharp corners"
        )

    # ---- render + exports ----------------------------------------------
    un_edges = []
    for net_name, pid_a, pid_b in result.failed:
        pa, pb = board.pads.get(pid_a), board.pads.get(pid_b)
        if pa and pb:
            un_edges.append(((pa.x, pa.y), (pb.x, pb.y), net_name))
    png = f"{out_base}.png"
    draw_board(
        board, png,
        traces=result.traces_by_layer(),
        vias=[(v.x, v.y, v.diameter) for v in result.vias],
        unrouted_edges=un_edges,
        title=f"{dsn_path} — routed {result.routed_edges}, "
              f"failed {len(result.failed)}, vias {len(result.vias)}",
    )
    ses = f"{out_base}.ses"
    write_ses(ses, dsn_path, board, result)
    with open(f"{out_base}.pkl", "wb") as fh:
        pickle.dump(
            {
                "dsn": dsn_path,
                "traces": [(t.net, t.layer, t.coords, t.width) for t in result.traces],
                "vias": [(v.net, v.x, v.y, v.diameter, v.padstack) for v in result.vias],
                "failed": result.failed,
                "diffpair_nets": result.diffpair_nets,
            },
            fh,
        )
    say(f"wrote {png} / {ses} / {out_base}.pkl")

    # ---- statistics ------------------------------------------------------
    len_by_layer: dict[str, float] = defaultdict(float)
    widths = []
    for t in result.traces:
        L = sum(
            math.hypot(q[0] - p[0], q[1] - p[1])
            for p, q in zip(t.coords, t.coords[1:])
        )
        len_by_layer[t.layer] += L
        widths.append(t.width)
    vias_by_size: dict[float, int] = defaultdict(int)
    for v in result.vias:
        vias_by_size[round(v.diameter, 1)] += 1
    b = board.outline.bounds
    total = result.routed_edges + len(result.failed)

    return {
        "dsn": dsn_path,
        "png": png,
        "ses": ses,
        "board": {
            "components": len({p.pin_id.split("-")[0] for p in board.pads.values()}),
            "pads": len(board.pads),
            "nets": len(board.nets),
            "layers": list(board.layers),
            "size_mil": (round(b[2] - b[0], 1), round(b[3] - b[1], 1)),
            "size_mm": (
                round((b[2] - b[0]) * 0.0254, 1),
                round((b[3] - b[1]) * 0.0254, 1),
            ),
        },
        "routing": {
            "routed": result.routed_edges,
            "total": total,
            "percent": round(100.0 * result.routed_edges / total, 2) if total else 0.0,
            "failed": list(result.failed),
            "vias": len(result.vias),
            "vias_by_size": dict(sorted(vias_by_size.items())),
            "trace_len_by_layer_mil": {
                k: round(v, 0) for k, v in sorted(len_by_layer.items())
            },
            "width_min": round(min(widths), 2) if widths else 0,
            "width_max": round(max(widths), 2) if widths else 0,
            "seconds": round(time.time() - t0, 1),
        },
        "quality": {
            "violations": violations,
            "open_nets": opens,
            "dangling": dangling,
            "sharp_corners": corners,
            "pairs": [
                {"p": p, "n": n, "coupled_pct": round(pct, 1), "uncoupled_mil": round(u, 0)}
                for p, n, pct, u in pairs
            ],
        },
    }
