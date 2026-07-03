"""Render selected nets from a saved routing result (.pkl), zoomed to fit.

Usage: python render_nets.py routed_4layer.pkl CANBUS_P CANBUS_N [-o out.png]
       (no nets = whole board)
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
ap.add_argument("nets", nargs="*")
ap.add_argument("-o", "--out", default="nets.png")
ap.add_argument("--margin", type=float, default=120.0)
args = ap.parse_args()

with open(args.pkl, "rb") as fh:
    data = pickle.load(fh)
board = load_dsn(data["dsn"])
want = set(args.nets) if args.nets else None

fig, ax = plt.subplots(figsize=(18, 14))
ax.set_facecolor("#111")
fig.patch.set_facecolor("#111")

# all pads faint; selected nets' pads bright
for pad in board.pads.values():
    sel = want is None or pad.net in want
    for layer in pad.layers():
        g = pad.geometry_on(layer)
        if g is None or not hasattr(g, "exterior"):
            continue
        ax.add_patch(
            MplPolygon(
                np.asarray(g.exterior.coords), closed=True,
                facecolor=layer_color(board, layer) if sel else "#3a3a3a",
                alpha=0.95 if sel else 0.5, zorder=3 if sel else 1,
            )
        )

xs, ys = [], []
# context: other nets' copper, dimmed
for net, layer, coords, width in data["traces"]:
    if want is not None and net in want:
        continue
    arr = np.asarray(coords)
    ax.plot(arr[:, 0], arr[:, 1], color="#555555", lw=max(0.8, width / 3.5),
            alpha=0.55, solid_capstyle="round", zorder=2)
for net, x, y, dia, ps in data["vias"]:
    if want is not None and net in want:
        continue
    ax.add_patch(plt.Circle((x, y), dia / 2, facecolor="#4a4a3a",
                            edgecolor="none", zorder=2))
for net, layer, coords, width in data["traces"]:
    if want is not None and net not in want:
        continue
    arr = np.asarray(coords)
    xs += [arr[:, 0].min(), arr[:, 0].max()]
    ys += [arr[:, 1].min(), arr[:, 1].max()]
    ax.plot(
        arr[:, 0], arr[:, 1], color=layer_color(board, layer),
        lw=max(1.2, width / 2.2), alpha=0.9,
        solid_capstyle="round", solid_joinstyle="round", zorder=5,
    )
for net, x, y, dia, ps in data["vias"]:
    if want is not None and net not in want:
        continue
    ax.add_patch(plt.Circle((x, y), dia / 2, facecolor=VIA_COLOR,
                            edgecolor="black", lw=0.4, zorder=6))
    xs += [x]
    ys += [y]

if want is not None:
    for pad in board.pads.values():
        if pad.net in want:
            xs.append(pad.x)
            ys.append(pad.y)

if xs:
    m = args.margin
    ax.set_xlim(min(xs) - m, max(xs) + m)
    ax.set_ylim(min(ys) - m, max(ys) + m)
ax.set_aspect("equal")
ax.set_title(", ".join(args.nets) if args.nets else "all nets", color="w")
fig.tight_layout()
fig.savefig(args.out, dpi=170, facecolor=fig.get_facecolor())
print(f"wrote {args.out}")
