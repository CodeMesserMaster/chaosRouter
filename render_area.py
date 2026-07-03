"""Render a zoomed area of a routed board around a component (all nets, full color).

Usage: python render_area.py routed_4layer.pkl U8 [-o u8.png] [--margin 150]
"""

import argparse
import pickle

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Polygon as MplPolygon

from chaosrouter import load_dsn
from chaosrouter.viz import VIA_COLOR, layer_color

ap = argparse.ArgumentParser()
ap.add_argument("pkl")
ap.add_argument("ref", help="component ref des, e.g. U8")
ap.add_argument("-o", "--out", default="area.png")
ap.add_argument("--margin", type=float, default=150.0)
args = ap.parse_args()

with open(args.pkl, "rb") as fh:
    data = pickle.load(fh)
board = load_dsn(data["dsn"])

pref = args.ref + "-"
own = [p for pid, p in board.pads.items() if pid.startswith(pref)]
if not own:
    raise SystemExit(f"no pads for {args.ref}")
m = args.margin
x0 = min(p.x for p in own) - m
x1 = max(p.x for p in own) + m
y0 = min(p.y for p in own) - m
y1 = max(p.y for p in own) + m

fig, ax = plt.subplots(figsize=(16, 16 * (y1 - y0) / (x1 - x0 + 1e-9)))
ax.set_facecolor("#111")
fig.patch.set_facecolor("#111")

for pad in board.pads.values():
    if not (x0 - 100 < pad.x < x1 + 100 and y0 - 100 < pad.y < y1 + 100):
        continue
    for layer in pad.layers():
        g = pad.geometry_on(layer)
        if g is None or not hasattr(g, "exterior"):
            continue
        ax.add_patch(
            MplPolygon(
                np.asarray(g.exterior.coords), closed=True,
                facecolor=layer_color(board, layer), alpha=0.55, zorder=2,
            )
        )

for net, layer, coords, width in data["traces"]:
    arr = np.asarray(coords)
    if arr[:, 0].max() < x0 - 50 or arr[:, 0].min() > x1 + 50:
        continue
    if arr[:, 1].max() < y0 - 50 or arr[:, 1].min() > y1 + 50:
        continue
    # linewidth in data units: transform width (mil) to points via axes scale
    ax.plot(
        arr[:, 0], arr[:, 1], color=layer_color(board, layer),
        lw=width * 16 * 72 / (x1 - x0) / 1.35, alpha=0.85,
        solid_capstyle="round", solid_joinstyle="round", zorder=4,
    )
for net, x, y, dia, ps in data["vias"]:
    if x0 - 50 < x < x1 + 50 and y0 - 50 < y < y1 + 50:
        ax.add_patch(plt.Circle((x, y), dia / 2, facecolor=VIA_COLOR,
                                edgecolor="black", lw=0.4, zorder=6))

ax.set_xlim(x0, x1)
ax.set_ylim(y0, y1)
ax.set_aspect("equal")
ax.set_title(args.ref, color="w")
fig.tight_layout()
fig.savefig(args.out, dpi=140, facecolor=fig.get_facecolor())
print(f"wrote {args.out}")
