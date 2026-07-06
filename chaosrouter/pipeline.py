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
    on_add=None,
    on_rip=None,
    include_geometry: bool = False,
    strict_width: bool = False,
    avoid_padstacks=(),
    method: str = "chaos",
    via_map: dict | None = None,
    persist_seconds: float = 0.0,
    curve_method: str = "fillet",
) -> dict:
    """Route a DSN end to end. `progress(line: str)` receives log lines;
    `on_add`/`on_rip` receive live copper events (for GUI animation).
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
    ws.on_add = on_add
    ws.on_rip = on_rip
    router = Router(
        board, ws, power_sources=sources or {}, strict_width=strict_width,
        avoid_padstacks=frozenset(avoid_padstacks or ()),
    )

    # Progress spanning ALL phases: the first pass fills 0->40% (fast, does
    # the bulk of connections), then the completion tail (rip-up/shake/
    # endgame) fills 40->99% as it drives the failure count toward zero.
    # Monotonic so shake's temporary failure bumps never rewind the bar.
    st = {"peak": 0, "pct": 0.0}

    def rp(i, n, name, res):
        failed = len(res.failed)
        if isinstance(i, int) and n:            # first pass / realize: i of n
            pct = 40.0 * i / n
        elif failed > st["peak"] or st["peak"] > 0:
            # completion tail: advance as failures resolve toward zero
            if failed > st["peak"]:
                st["peak"] = failed
            pct = 40.0 + 59.0 * (1.0 - failed / st["peak"])
        else:
            # no failures recorded yet (e.g. pathfinder's negotiate phase):
            # hold — don't jump the bar to the end
            pct = st["pct"]
        pct = max(st["pct"], min(99.0, pct))
        st["pct"] = pct
        say(f"@P|{pct:.1f}|{res.routed_edges}|{failed}")
        if (isinstance(i, int) and n and (i % 10 == 0 or i == n)) or n == 0:
            say(
                f"[{i}/{n}] {name}  edges={res.routed_edges} "
                f"failed={failed} vias={len(res.vias)}"
            )

    if method == "pathfinder":
        from .pathfinder import route_all_pathfinder

        result = route_all_pathfinder(router, progress=rp)
    elif method == "manhattan":
        # Manhattan-structured (validated on dense BMS: 84.5% -> 91%, 100%
        # direction discipline). Directional HIGHWAYS on the INNER layers —
        # East-West on odd inner layers, North-South on even — while Top/Bottom
        # stay free for short local escapes, with a base cost that pushes LONG
        # routes down onto the inner highways. Strict grain (grain_pen>via/step)
        # forces a layer change to change direction. Curved by the fillet pass.
        sig = list(getattr(board, "signal_layers", None) or board.layers)
        # NOTE: router._orthogonal = True gives pure straight H/V lines but
        # costs ~20 connections on dense boards (diagonal shortcuts aid
        # routability). Default off — favour completion; set it for the look.
        if len(sig) >= 4:
            inner = sig[1:-1]
            router._grain = {ly: (i % 2) for i, ly in enumerate(inner)}
            router._grain_pen = 25.0
            router._layer_base = {sig[0]: 3.0, sig[-1]: 3.0}
        else:
            # 2-layer board: Top = East-West, Bottom = North-South
            router._grain = {sig[0]: 0, sig[-1]: 1}
            router._grain_pen = 25.0
        result = router.route_all(progress=rp)
    elif method == "manhattan-fanout":
        # EXPERIMENTAL: Manhattan + PLANNED FANOUT. Order: diff pairs ->
        # coordinated escape bundles for fine-pitch ICs (each pin escaped out
        # of the pad field to an aligned breakout, in pin order so they can't
        # cross) -> Manhattan routing, which now starts each escaped pin from
        # its breakout OUTSIDE the field instead of fighting to get out.
        from .diffpair import find_diff_pairs, route_diff_pair
        from .fanout import planned_fanout

        sig = list(getattr(board, "signal_layers", None) or board.layers)
        if len(sig) >= 4:
            inner = sig[1:-1]
            router._grain = {ly: (i % 2) for i, ly in enumerate(inner)}
            router._grain_pen = 25.0
            router._layer_base = {sig[0]: 3.0, sig[-1]: 3.0}
        else:
            router._grain = {sig[0]: 0, sig[-1]: 1}
            router._grain_pen = 25.0
        for net_p, net_n, gap in find_diff_pairs(board):
            if route_diff_pair(router, net_p, net_n, gap):
                router.result.diffpair_nets |= {net_p.name, net_n.name}
        ne = planned_fanout(router, progress=rp)
        say(f"planned fanout: {ne} pins escaped from fine-pitch ICs")
        nets = [
            n for n in router.net_order()
            if n.name not in router.result.diffpair_nets
        ]
        for net in nets:
            router.route_net(net)
        router._rip_and_retry(rp)
        router._shake_parallel(rp)
        router._endgame(rp)
        if router.result.failed:
            router._shake_parallel(rp)
        result = router.result
    else:
        result = router.route_all(progress=rp, persist_seconds=persist_seconds)
    t_route = time.time() - t0
    say(
        f"routing done: {result.routed_edges} connections, "
        f"{len(result.failed)} failed, {len(result.vias)} vias ({t_route:.0f}s)"
    )

    ws.on_add = ws.on_rip = None  # style passes re-register copper
    pruned = router.prune_open_stubs()
    grafts = router.beautify_exits()
    if curve_method == "relax":
        # field relaxation IS the curve engine (fast hybrid: grid-field
        # forces + exact legality). Teardrops are SKIPPED here — the fatten
        # pass fragments traces and would reintroduce corners into the
        # smooth curves.
        from .fastfield import relax_hybrid

        say("relaxing traces into curves (fast field solver) ...")
        relax_hybrid(router, result, iters=90, progress=say)
        # fillet the traces relax couldn't flow (boxed-in): flowing curves
        # in the open, tangent-arc fillets in tight spots — never angular
        fillet_result(result, ws, board, r_target=fillet_r)
        say(f"style: {pruned} stubs pruned, {grafts} exits grafted, "
            "relaxed + filleted (teardrops skipped in relax mode)")
    else:
        fat = router.fatten_pad_entries()
        say(f"style: {pruned} stubs pruned, {grafts} exits grafted, {fat} fattened")
        say("filleting corners into arcs ...")
        fillet_result(result, ws, board, r_target=fillet_r)

    # redraw the live view with the FINAL styled geometry — teardrop pad
    # entries, graded neck-downs and filleted arcs. The route streamed in
    # raw (style passes run with the on_add hook off), so without this the
    # board view never shows the tapered/teardropped result.
    if on_add:
        say("@CLEAR")
        for t in result.traces:
            on_add("trace", t.net, t.layer, t.coords, t.width)
        for v in result.vias:
            on_add("via", v.net, v.x, v.y, v.diameter)

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
    write_ses(ses, dsn_path, board, result, via_map=via_map)
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

    geometry = None
    if include_geometry:
        geometry = {
            "traces": [
                [t.net, t.layer, [[round(x, 2), round(y, 2)] for x, y in t.coords],
                 round(t.width, 3)]
                for t in result.traces
            ],
            "vias": [
                [v.net, round(v.x, 2), round(v.y, 2), round(v.diameter, 2)]
                for v in result.vias
            ],
            # unrouted connections (failed edges) as pad-to-pad segments, so
            # the GUI can show them in red for placement feedback
            "unrouted": [
                [round(a[0], 2), round(a[1], 2), round(b[0], 2), round(b[1], 2), net]
                for a, b, net in un_edges
            ],
        }

    return {
        "dsn": dsn_path,
        "png": png,
        "ses": ses,
        "geometry": geometry,
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
