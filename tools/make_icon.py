"""Generate the chaosRouter app icon (PNG + ICO + ICNS).

Concept: order out of chaos — thin chaotic traces in the layer palette
resolve into one bold curved trace sweeping a "C", ending in a gold via.
"""

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch

OUT = os.path.join(os.path.dirname(__file__), "..", "chaosrouter", "assets")
os.makedirs(OUT, exist_ok=True)

BG = "#0b0b0d"
ACCENT = "#f5a623"
PALETTE = ["#e05555", "#31b0d5", "#3fbf6f", "#8a6cf0"]
GOLD = "#e8c34a"


def bezier(p0, p1, p2, p3, n=120):
    t = np.linspace(0, 1, n)[:, None]
    pts = ((1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1
           + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3)
    return pts[:, 0], pts[:, 1]


fig = plt.figure(figsize=(8, 8), dpi=64)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.axis("off")
fig.patch.set_alpha(0.0)

# rounded dark tile
tile = FancyBboxPatch(
    (4, 4), 92, 92, boxstyle="round,pad=0,rounding_size=20",
    facecolor=BG, edgecolor="#26262e", linewidth=2,
)
ax.add_patch(tile)

rng = np.random.default_rng(23)
# background chaos: thin curved traces wandering, each ending in a tiny via
for i, col in enumerate(PALETTE * 2):
    p0 = np.array([rng.uniform(12, 88), rng.uniform(12, 88)])
    p3 = np.array([rng.uniform(12, 88), rng.uniform(12, 88)])
    p1 = p0 + rng.uniform(-38, 38, 2)
    p2 = p3 + rng.uniform(-38, 38, 2)
    x, y = bezier(p0, p1, p2, p3)
    keep = (x > 16) & (x < 84) & (y > 16) & (y < 84)
    (line,) = ax.plot(x[keep], y[keep], color=col, lw=2.2, alpha=0.5,
                      solid_capstyle="round")
    line.set_clip_path(tile)
    if keep.any():
        dot = plt.Circle((x[keep][-1], y[keep][-1]), 1.8, color=col, alpha=0.6)
        ax.add_patch(dot)
        dot.set_clip_path(tile)

# the hero trace: a bold smooth "C" sweep with a glow
theta = np.linspace(np.radians(35), np.radians(325), 200)
r = 30
cx, cy = 50, 50
hx = cx + r * np.cos(theta)
hy = cy + r * np.sin(theta)
for lw, alpha in ((16, 0.12), (12, 0.2), (9, 1.0)):
    ax.plot(hx, hy, color=ACCENT, lw=lw, alpha=alpha, solid_capstyle="round")

# teardrop entry into a gold via at the top end of the C
end = (hx[0], hy[0])
ax.add_patch(plt.Circle(end, 7.5, color=GOLD))
ax.add_patch(plt.Circle(end, 3.2, color=BG))
# small via at the tail end too
tail = (hx[-1], hy[-1])
ax.add_patch(plt.Circle(tail, 5.2, color=GOLD))
ax.add_patch(plt.Circle(tail, 2.2, color=BG))

png512 = os.path.join(OUT, "icon_512.png")
fig.set_dpi(64)
fig.savefig(png512, transparent=True)
plt.close(fig)

# multi-size ICO + ICNS via Pillow
from PIL import Image

img = Image.open(png512)
img.save(os.path.join(OUT, "icon.ico"),
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                (128, 128), (256, 256)])
img.save(os.path.join(OUT, "icon.icns"))
print("wrote", OUT, os.listdir(OUT))
