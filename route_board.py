"""Route the board end to end: parse -> route -> fillet -> DRC -> render -> SES."""

import argparse
import time

from chaosrouter import load_dsn
from chaosrouter.curves import fillet_result
from chaosrouter.drc import check
from chaosrouter.grid import Workspace
from chaosrouter.router import Router
from chaosrouter.ses import write_ses
from chaosrouter.viz import draw_board, ratsnest_edges

ap = argparse.ArgumentParser()
ap.add_argument("dsn", nargs="?", default="jbx_4g_r4.dsn")
ap.add_argument("--step", type=float, default=4.0, help="grid step, mil")
ap.add_argument("--limit", type=int, default=0, help="route only first N nets (0=all)")
ap.add_argument("--fillet", type=float, default=25.0, help="max fillet radius, mil")
ap.add_argument("--no-drc", action="store_true")
ap.add_argument("--out", default="routed")
ap.add_argument(
    "--source", action="append", default=[], metavar="NET=PIN",
    help="star-connection source pin for a power net, e.g. --source 5v=U9-2",
)
args = ap.parse_args()
sources = dict(s.split("=", 1) for s in args.source)

t0 = time.time()
board = load_dsn(args.dsn)
print(board.stats())

ws = Workspace(board, step=args.step)
print(f"grid: {ws.nx} x {ws.ny} cells/layer @ {args.step} mil  ({time.time()-t0:.1f}s)")

router = Router(board, ws, power_sources=sources)
if args.limit:
    order = router.net_order()[: args.limit]
    router.net_order = lambda: order  # route only a subset


def progress(i, n, name, res):
    if i % 10 == 0 or i == n:
        print(
            f"  [{i}/{n}] {name:30s} edges={res.routed_edges} "
            f"failed={len(res.failed)} vias={len(res.vias)} ({time.time()-t0:.0f}s)"
        )


result = router.route_all(progress=progress)
print(f"routing done: {result.routed_edges} connections, "
      f"{len(result.failed)} failed, {len(result.vias)} vias  ({time.time()-t0:.0f}s)")
if result.failed:
    for net, a, b in result.failed:
        print(f"    FAILED {net}: {a} <-> {b}")

pruned = router.prune_open_stubs()
print(f"pruned {pruned} open trace stubs")
grafts = router.beautify_exits()
print(f"beautify: {grafts} straight pad exits grafted")
fat = router.fatten_pad_entries()
print(f"beautify: {fat} pad entries fattened to pad width")
print("filleting corners into arcs...")
fillet_result(result, ws, board, r_target=args.fillet)

from chaosrouter.drc import check_pairs

for p_name, n_name, pct, uncoupled in check_pairs(board, result):
    print(f"PAIR {p_name}/{n_name}: {pct:.1f}% coupled, {uncoupled:.0f} mil uncoupled")

if not args.no_drc:
    print("running DRC (exact geometry)...")
    violations, opens = check(board, result)
    print(f"DRC: {len(violations)} clearance violations, {len(opens)} open nets")
    for v in violations[:15]:
        print(f"    {v['layer']}: {v['a']} vs {v['b']} gap={v['gap']} need={v['need']} at {v['where']}")
    for name, parts in opens[:15]:
        print(f"    OPEN {name}: {parts} disconnected islands")

    from chaosrouter.drc import check_geometry

    dangling, corners = check_geometry(board, result)
    print(f"geometry: {len(dangling)} dangling trace ends, "
          f"{len(corners)} sharp corners (>30 deg)")
    for d in dangling[:10]:
        print(f"    DANGLING {d['net']} {d['layer']} at {d['where']}")
    for c in corners[:10]:
        print(f"    CORNER {c['net']} {c['layer']} at {c['where']} turn={c['turn']}")

# ratsnest: only the actually-failed connections
un_edges = []
for net_name, pid_a, pid_b in result.failed:
    pa, pb = board.pads.get(pid_a), board.pads.get(pid_b)
    if pa and pb:
        un_edges.append(((pa.x, pa.y), (pb.x, pb.y), net_name))

draw_board(
    board,
    f"{args.out}.png",
    traces=result.traces_by_layer(),
    vias=[(v.x, v.y, v.diameter) for v in result.vias],
    unrouted_edges=un_edges,
    title=f"{args.dsn} — routed {result.routed_edges}, failed {len(result.failed)}, vias {len(result.vias)}",
)
print(f"wrote {args.out}.png")

write_ses(f"{args.out}.ses", args.dsn, board, result)

import pickle

with open(f"{args.out}.pkl", "wb") as fh:
    pickle.dump(
        {
            "dsn": args.dsn,
            "traces": [(t.net, t.layer, t.coords, t.width) for t in result.traces],
            "vias": [(v.net, v.x, v.y, v.diameter, v.padstack) for v in result.vias],
            "failed": result.failed,
            "diffpair_nets": result.diffpair_nets,
        },
        fh,
    )
print(f"wrote {args.out}.ses / .pkl   total {time.time()-t0:.0f}s")
