"""Drive the Rust parallel routing engine on a real board."""
import sys, time
sys.path.insert(0, "D:/diptrace_router")
import chaosrouter_rs as rs
from chaosrouter import load_dsn


def build_and_route(dsn):
    board = load_dsn(dsn)
    layer_id = {ly: i for i, ly in enumerate(board.layers)}
    net_id = {n: i for i, n in enumerate(board.nets)}
    b = board.outline.bounds

    pads = []              # (net_id, layer, xs, ys, clr)
    terminal_of = {}       # board pad key -> first entry index (its terminal)
    centre_of = {}
    for pid, pad in board.pads.items():
        centre_of[pid] = (pad.x, pad.y)
        netc = board.nets.get(pad.net)
        clr = netc.clearance if netc else board.default_clearance
        # no-net pads (holes, mounting) block everyone -> sentinel -2
        nid = net_id.get(pad.net, -2) if pad.net else -2
        first = None
        for ly in pad.layers():
            g = pad.geometry_on(ly)
            if g is None or not hasattr(g, "exterior"):
                continue
            xs = [p[0] for p in g.exterior.coords]
            ys = [p[1] for p in g.exterior.coords]
            if first is None:
                first = len(pads)
            pads.append((nid, layer_id[ly], xs, ys, clr))
        if first is not None:
            terminal_of[pid] = first

    # net jobs: chain each net's pads nearest-neighbour from the first
    key_of = {id(p): f"{p.ref}-{p.pin}" for p in board.pads.values()}
    jobs = []
    for name, net in board.nets.items():
        pad_ids = [k for p in board.pads_of_net(net)
                   for k in [key_of[id(p)]] if k in terminal_of]
        if len(pad_ids) < 2:
            continue
        order = [pad_ids[0]]
        rest = set(pad_ids[1:])
        while rest:
            cx, cy = centre_of[order[-1]]
            nxt = min(rest, key=lambda p: (centre_of[p][0]-cx)**2 + (centre_of[p][1]-cy)**2)
            order.append(nxt); rest.discard(nxt)
        term = [terminal_of[p] for p in order]
        jobs.append((net_id[name], term, net.width, net.clearance))

    ext = list(board.outline.exterior.coords)
    ox = [p[0] for p in ext]; oy = [p[1] for p in ext]
    print(f"board: {len(board.layers)} layers, {len(pads)} pad-faces, {len(jobs)} nets to route")
    traces, vias, secs, threads = rs.route_board(
        len(board.layers), b[0], b[1], b[2], b[3], 4.0, ox, oy, pads, jobs)
    print(f"RUST route_board: {len(traces)} trace-segments in {secs:.2f}s "
          f"across {threads} threads")
    return board, traces, vias, secs


if __name__ == "__main__":
    build_and_route("jbx_4g_r4.dsn")
