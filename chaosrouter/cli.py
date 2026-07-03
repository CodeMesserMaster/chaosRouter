"""chaosRouter command-line interface.

--stream emits machine-readable live events on stdout (used by the GUI):
    @T|net|layer|width|x,y;x,y;...   trace added
    @V|net|x|y|dia                   via added
    @R|net                           net ripped (remove its live copper)
--stats-json writes the final statistics dict as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser(
        prog="chaosrouter",
        description="chaosRouter — curved-trace PCB autorouter for Specctra DSN exports",
    )
    ap.add_argument("dsn", help="input .dsn file")
    ap.add_argument("--out", default="routed", help="output base name")
    ap.add_argument("--step", type=float, default=4.0, help="grid step, mil")
    ap.add_argument("--fillet", type=float, default=25.0, help="max fillet radius, mil")
    ap.add_argument("--no-drc", action="store_true")
    ap.add_argument("--stream", action="store_true", help="emit live copper events")
    ap.add_argument(
        "--strict-width", action="store_true",
        help="never route below class width (DipTrace-safe: its SES import "
             "normalizes widths back to the class width)",
    )
    ap.add_argument(
        "--avoid-via", action="append", default=None, metavar="PADSTACK",
        help="never place this via padstack (default: inPadVia, which "
             "DipTrace imports oversized); pass --allow-all-vias to clear",
    )
    ap.add_argument("--allow-all-vias", action="store_true")
    ap.add_argument("--stats-json", default=None, help="write stats JSON here")
    ap.add_argument(
        "--source", action="append", default=[], metavar="NET=PIN",
        help="star-connection source pin for a power net, e.g. --source 5v=U9-2",
    )
    args = ap.parse_args()

    def say(line: str):
        print(line, flush=True)

    on_add = on_rip = None
    if args.stream:

        def on_add(kind, net, *rest):
            if kind == "trace":
                layer, coords, width = rest
                pts = ";".join(f"{x:.2f},{y:.2f}" for x, y in coords)
                print(f"@T|{net}|{layer}|{width:.3f}|{pts}", flush=True)
            else:
                x, y, dia = rest
                print(f"@V|{net}|{x:.2f}|{y:.2f}|{dia:.2f}", flush=True)

        def on_rip(net):
            print(f"@R|{net}", flush=True)

    from .pipeline import run_pipeline

    stats = run_pipeline(
        args.dsn,
        out_base=args.out,
        step=args.step,
        fillet_r=args.fillet,
        sources=dict(s.split("=", 1) for s in args.source),
        drc=not args.no_drc,
        progress=say,
        on_add=on_add,
        on_rip=on_rip,
        strict_width=args.strict_width,
        avoid_padstacks=(
            () if args.allow_all_vias
            else tuple(args.avoid_via) if args.avoid_via else ("inPadVia",)
        ),
        include_geometry=bool(args.stats_json),
    )
    if args.stats_json:
        with open(args.stats_json, "w", encoding="utf-8") as fh:
            json.dump(stats, fh)
    r = stats["routing"]
    print(
        f"\nchaosRouter: {r['routed']}/{r['total']} connections "
        f"({r['percent']}%), {r['vias']} vias, "
        f"{len(stats['quality']['violations'])} DRC violations, "
        f"{r['seconds']:.0f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
