"""chaosRouter command-line interface."""

from __future__ import annotations

import argparse


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
    ap.add_argument(
        "--source", action="append", default=[], metavar="NET=PIN",
        help="star-connection source pin for a power net, e.g. --source 5v=U9-2",
    )
    args = ap.parse_args()

    from .pipeline import run_pipeline

    stats = run_pipeline(
        args.dsn,
        out_base=args.out,
        step=args.step,
        fillet_r=args.fillet,
        sources=dict(s.split("=", 1) for s in args.source),
        drc=not args.no_drc,
        progress=print,
    )
    r = stats["routing"]
    print(
        f"\nchaosRouter: {r['routed']}/{r['total']} connections "
        f"({r['percent']}%), {r['vias']} vias, "
        f"{len(stats['quality']['violations'])} DRC violations, "
        f"{r['seconds']:.0f}s"
    )


if __name__ == "__main__":
    main()
