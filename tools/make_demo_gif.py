"""Generate docs/demo.gif — chaosRouter routing a synthetic board, live.

Runs the real pipeline on a generated board, records copper events, and
renders them as cumulative animation frames.
"""

import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from boards import simple_board

from chaosrouter import load_dsn
from chaosrouter.pipeline import run_pipeline

ROOT = os.path.join(os.path.dirname(__file__), "..")
DOCS = os.path.join(ROOT, "docs")
os.makedirs(DOCS, exist_ok=True)

BG = "#0b0b0d"
LAYER_COLORS = {"Top": "#f5a623", "In1": "#31b0d5", "In2": "#3fbf6f",
                "Bottom": "#e05555"}
VIA = "#e8c34a"

import tempfile

work = tempfile.mkdtemp(prefix="chaos_demo_")
dsn = os.path.join(work, "demo.dsn")
with open(dsn, "w") as fh:
    fh.write(
        simple_board(n_pairs=8, layers=("Top", "In1", "In2", "Bottom"),
                     wall=True, diff_pair=True)
    )

events = []
run_pipeline(
    dsn, out_base=os.path.join(work, "demo_routed"), drc=False,
    on_add=lambda kind, *a: events.append((kind, a)),
    on_rip=lambda net: events.append(("rip", (net,))),
)
print(f"{len(events)} copper events recorded")

board = load_dsn(dsn)

frames = []
per_frame = max(1, len(events) // 36)


def new_fig():
    fig = plt.figure(figsize=(6, 4.8), dpi=110)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG)
    fig.patch.set_facecolor(BG)
    ax.axis("off")
    b = board.outline.bounds
    ax.set_xlim(b[0] - 40, b[2] + 40)
    ax.set_ylim(b[1] - 40, b[3] + 40)
    ax.plot(*board.outline.exterior.xy, color="#3a3a42", lw=1.5)
    for pad in board.pads.values():
        for layer in pad.layers():
            g = pad.geometry_on(layer)
            if g is not None and hasattr(g, "exterior"):
                ax.fill(*g.exterior.xy,
                        color=LAYER_COLORS.get(layer, "#888"), alpha=0.4)
    return fig, ax


live = {}  # net -> list of artists' data (redraw each frame is simpler)
copper = []  # committed (kind, args) sequence with rips applied


def render(seq):
    fig, ax = new_fig()
    for kind, a in seq:
        if kind == "trace":
            net, layer, coords, width = a
            xs, ys = zip(*coords)
            ax.plot(xs, ys, color=LAYER_COLORS.get(layer, "#888"),
                    lw=width * 0.65, alpha=0.95, solid_capstyle="round")
        elif kind == "via":
            net, x, y, dia = a
            ax.add_patch(plt.Circle((x, y), dia / 2, facecolor=VIA,
                                    edgecolor="black", lw=0.4))
    fig.canvas.draw()
    w, h = fig.canvas.get_width_height()
    img = Image.frombuffer("RGBA", (w, h), fig.canvas.buffer_rgba())
    plt.close(fig)
    return img.convert("P", palette=Image.ADAPTIVE)


seq = []
for i, (kind, a) in enumerate(events):
    if kind == "rip":
        net = a[0]
        seq = [(k, aa) for k, aa in seq
               if not (k in ("trace", "via") and aa[0] == net)]
    else:
        seq.append((kind, a))
    if i % per_frame == 0 or i == len(events) - 1:
        frames.append(render(seq))

# hold the finished board for a moment
frames += [frames[-1]] * 8

out = os.path.join(DOCS, "demo.gif")
frames[0].save(
    out, save_all=True, append_images=frames[1:], duration=120, loop=0,
    optimize=True,
)
print(f"wrote {out} ({os.path.getsize(out)//1024} kB, {len(frames)} frames)")
