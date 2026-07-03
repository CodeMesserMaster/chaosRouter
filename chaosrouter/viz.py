"""Board / routing visualization with matplotlib."""

from __future__ import annotations

import numpy as np
from shapely.geometry import LineString
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.patches import Polygon as MplPolygon

from .model import Board

LAYER_COLORS = ["#e5484d", "#3e63dd", "#30a46c", "#f5a623", "#9d5cd0", "#12a5a5"]
RATS_COLOR = "#8f8f8f"
OUTLINE_COLOR = "#e0e0e0"
VIA_COLOR = "#c7b04c"


def layer_color(board, layer: str) -> str:
    try:
        return LAYER_COLORS[board.layers.index(layer) % len(LAYER_COLORS)]
    except ValueError:
        return "#888888"


def _geom_patches(geom, **kw):
    """Yield matplotlib patches for a shapely (multi)polygon."""
    geoms = getattr(geom, "geoms", [geom])
    for g in geoms:
        if g.is_empty or not hasattr(g, "exterior"):
            continue
        yield MplPolygon(np.asarray(g.exterior.coords), closed=True, **kw)


def ratsnest_edges(board: Board) -> list[tuple[tuple, tuple, str]]:
    """MST edges (p1, p2, net_name) over each net's pad positions."""
    from scipy.sparse.csgraph import minimum_spanning_tree
    from scipy.spatial.distance import squareform, pdist

    edges = []
    for net in board.nets.values():
        pads = board.pads_of_net(net)
        if len(pads) < 2:
            continue
        pts = np.array([(p.x, p.y) for p in pads])
        dm = squareform(pdist(pts))
        mst = minimum_spanning_tree(dm).tocoo()
        for i, j in zip(mst.row, mst.col):
            edges.append((tuple(pts[i]), tuple(pts[j]), net.name))
    return edges


def draw_board(
    board: Board,
    out_path: str,
    traces: dict[str, list] | None = None,
    vias: list | None = None,
    show_ratsnest: bool = True,
    unrouted_edges: list | None = None,
    title: str = "",
    dpi: int = 200,
):
    """Render board. traces: {layer: [(coords_list, width, net), ...]},
    vias: [(x, y, diameter), ...], unrouted_edges overrides full ratsnest."""
    fig, ax = plt.subplots(figsize=(22, 18))
    ax.set_facecolor("#111111")
    fig.patch.set_facecolor("#111111")

    if board.outline is not None:
        x, y = board.outline.exterior.xy
        ax.plot(x, y, color=OUTLINE_COLOR, lw=1.2, zorder=10)

    # pads + traces per layer (draw layers bottom-up so Top ends on top)
    layer_order = list(reversed(board.layers))
    for zi, layer in enumerate(layer_order):
        color = layer_color(board, layer)
        patches = []
        for pad in board.pads.values():
            if layer not in pad.layers():
                continue
            geom = pad.geometry_on(layer)
            if geom is not None:
                patches.extend(_geom_patches(geom))
        ax.add_collection(
            PatchCollection(patches, facecolor=color, edgecolor="none", alpha=0.9, zorder=2 + zi)
        )
        if traces:
            # TRUE-SCALE widths: buffer the centerline by width/2 so the
            # image never under-draws copper (a points-based linewidth used
            # to show traces at ~half size and mislead width judgements)
            tpatches = []
            for coords, width, _net in traces.get(layer, []):
                try:
                    poly = LineString(coords).buffer(width / 2, quad_segs=6)
                    tpatches.extend(_geom_patches(poly))
                except Exception:
                    continue
            ax.add_collection(
                PatchCollection(
                    tpatches, facecolor=color, edgecolor="none", alpha=0.85,
                    zorder=2 + len(layer_order) + zi,
                )
            )

    if vias:
        for x, y, d in vias:
            ax.add_patch(
                plt.Circle((x, y), d / 2, facecolor=VIA_COLOR, edgecolor="black",
                           lw=0.3, zorder=6)
            )

    edges = unrouted_edges if unrouted_edges is not None else (
        ratsnest_edges(board) if show_ratsnest else []
    )
    if edges:
        segs = [(e[0], e[1]) for e in edges]
        ax.add_collection(
            LineCollection(segs, colors=RATS_COLOR, linewidths=0.5, alpha=0.8, zorder=7)
        )

    ax.set_aspect("equal")
    ax.autoscale()
    ax.set_title(title, color="white", fontsize=14)
    ax.tick_params(colors="#777777", labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#444444")
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
