"""Specctra DSN file -> Board model."""

from __future__ import annotations

from shapely.geometry import LineString, Point, Polygon, box

from . import sexp
from .model import (
    Board,
    Component,
    Net,
    NetClass,
    Pad,
    PadShape,
    Padstack,
    transform_point,
)


# mil per DSN coordinate unit — set by load_dsn from (unit ...) /
# (resolution ...). DipTrace exports mil; KiCad exports um.
_SCALE = 1.0

_UNIT_TO_MIL = {
    "inch": 1000.0,
    "mil": 1.0,
    "cm": 393.7007874,
    "mm": 39.37007874,
    "um": 0.03937007874,
}

# net-class rule values that are lengths and must be unit-scaled
_LENGTH_RULES = {
    "width", "clearance", "neck_down_width", "neck_down_gap",
    "diffpair_line_width", "diffpair_gap", "gap", "edge_primary_gap",
    "max_uncoupled_length", "edge_coupled_tolerance_minus",
    "edge_coupled_tolerance_plus", "phase_tolerance_length",
}


def _f(tok: str) -> float:
    return float(tok) * _SCALE


def _parse_shape(shape_node: list) -> PadShape | None:
    """shape_node like (circle Top 19.68) / (rect Top x1 y1 x2 y2) /
    (polygon Top width x y ...) / (path Top width x y ...)"""
    kind = shape_node[0]
    layer = shape_node[1]
    args = [_f(t) for t in shape_node[2:] if not isinstance(t, list)]
    if kind == "circle":
        d = args[0]
        cx, cy = (args[1], args[2]) if len(args) >= 3 else (0.0, 0.0)
        return PadShape(layer, Point(cx, cy).buffer(d / 2, quad_segs=16))
    if kind == "rect":
        x1, y1, x2, y2 = args[:4]
        return PadShape(layer, box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    if kind in ("polygon", "path"):
        width = args[0]
        coords = list(zip(args[1::2], args[2::2]))
        if kind == "polygon":
            geom = Polygon(coords)
            if width > 0:
                geom = geom.buffer(width / 2, quad_segs=8)
        else:
            if len(coords) < 2:
                geom = Point(coords[0]).buffer(max(width, 0.01) / 2)
            else:
                geom = LineString(coords).buffer(max(width, 0.01) / 2, quad_segs=8)
        return PadShape(layer, geom)
    return None


def load_dsn(path: str) -> Board:
    global _SCALE
    with open(path, encoding="utf-8", errors="replace") as fh:
        root = sexp.parse(fh.read())
    if str(root[0]).lower() != "pcb":
        raise ValueError(f"not a DSN PCB file: {root[0]}")

    # units: coordinates are expressed in (unit X), falling back to the
    # unit named by (resolution X n). DipTrace exports mil, KiCad um.
    _SCALE = 1.0
    unit_node = sexp.find(root, "unit")
    res_node = sexp.find(root, "resolution")
    unit = (unit_node[1] if unit_node else res_node[1] if res_node else "mil")
    _SCALE = _UNIT_TO_MIL.get(str(unit).lower(), 1.0)

    board = Board()

    # ---- structure ----------------------------------------------------
    structure = sexp.find(root, "structure")
    boundary_poly = None
    boundary_rect = None
    for bnode in sexp.find_all(structure, "boundary"):
        inner = bnode[1]
        if inner[0] == "rect":
            x1, y1, x2, y2 = (_f(t) for t in inner[2:6])
            boundary_rect = box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        elif inner[0] == "path":
            args = [_f(t) for t in inner[2:]]
            coords = list(zip(args[1::2], args[2::2]))  # skip aperture width
            if len(coords) >= 3:
                boundary_poly = Polygon(coords)
    board.outline = boundary_poly or boundary_rect

    layer_nodes = sexp.find_all(structure, "layer")
    board.layers = [n[1] for n in layer_nodes]
    # layer types: "power" == high-current plane (NOT routable for signals).
    # Everything else (mixed/signal) is a routable signal layer. The router
    # must only route signals on signal_layers; planes are handled as copper
    # pours by the CAD.
    board.layer_type = {}
    for n in layer_nodes:
        tnode = sexp.find(n, "type")
        board.layer_type[n[1]] = tnode[1] if tnode and len(tnode) > 1 else "signal"
    board.signal_layers = [
        ly for ly in board.layers if board.layer_type.get(ly) != "power"
    ]
    # plane nets: assigned to power-type (plane) layers — they are copper
    # pours, NOT routed as signal traces. Excluded from routing.
    board.plane_nets = set()
    for n in layer_nodes:
        if board.layer_type.get(n[1]) == "power":
            un = sexp.find(n, "use_net")
            if un:
                board.plane_nets.update(t for t in un[1:] if isinstance(t, str))
    for pl in sexp.find_all(structure, "plane"):
        if len(pl) > 1 and isinstance(pl[1], str):
            board.plane_nets.add(pl[1])
    via_node = sexp.find(structure, "via")
    if via_node:
        board.via_padstacks = [t for t in via_node[1:] if not isinstance(t, list)]
    for rule in sexp.find_all(structure, "rule"):
        for sub in rule[1:]:
            if isinstance(sub, list) and sub[0] == "width":
                board.default_width = _f(sub[1])
            elif isinstance(sub, list) and sub[0] == "clearance" and len(sub) == 2:
                board.default_clearance = _f(sub[1])

    # ---- placement -----------------------------------------------------
    placement = sexp.find(root, "placement")
    for comp_node in sexp.find_all(placement, "component"):
        image_name = comp_node[1]
        for pl in sexp.find_all(comp_node, "place"):
            ref = pl[1]
            board.components[ref] = Component(
                ref=ref,
                image_name=image_name,
                x=_f(pl[2]),
                y=_f(pl[3]),
                side=pl[4],
                rotation=_f(pl[5]) if len(pl) > 5 else 0.0,
            )

    # ---- padstacks (parse before images) --------------------------------
    library = sexp.find(root, "library")
    for ps_node in sexp.find_all(library, "padstack"):
        name = ps_node[1]
        ps = Padstack(name)
        for sh in sexp.find_all(ps_node, "shape"):
            parsed = _parse_shape(sh[1])
            if parsed is None:
                continue
            if parsed.layer == "signal":
                # Specctra wildcard: shape applies to every signal layer
                for ly in board.layers:
                    ps.shapes.append(PadShape(ly, parsed.geometry))
            else:
                ps.shapes.append(parsed)
        board.padstacks[name] = ps

    # ---- images -> pads --------------------------------------------------
    images: dict[str, list] = {}
    for img_node in sexp.find_all(library, "image"):
        images[img_node[1]] = img_node

    for comp in board.components.values():
        img = images.get(comp.image_name)
        if img is None:
            continue
        for out in sexp.find_all(img, "outline"):
            comp.outline.append(out[1])
        mirrored = comp.side == "back"
        for pin_node in sexp.find_all(img, "pin"):
            ps_name, pin_name = pin_node[1], pin_node[2]
            px, py = _f(pin_node[3]), _f(pin_node[4])
            ps = board.padstacks.get(ps_name)
            if ps is None:
                continue
            x, y = transform_point(px, py, comp.x, comp.y, comp.rotation, mirrored)
            pad = Pad(
                ref=comp.ref,
                pin=pin_name,
                padstack=ps,
                x=x,
                y=y,
                rotation=comp.rotation,
                mirrored=mirrored,
                board_layers=tuple(board.layers),
            )
            board.pads[pad.pin_id] = pad

    # ---- network ---------------------------------------------------------
    network = sexp.find(root, "network")
    for net_node in sexp.find_all(network, "net"):
        name = net_node[1]
        pins_node = sexp.find(net_node, "pins")
        pad_ids = list(pins_node[1:]) if pins_node else []
        board.nets[name] = Net(name=name, pad_ids=pad_ids)

    for cls_node in sexp.find_all(network, "class"):
        name = cls_node[1]
        nets = [t for t in cls_node[2:] if not isinstance(t, list)]
        nc = NetClass(name=name, nets=nets)
        circuit = sexp.find(cls_node, "circuit")
        if circuit:
            uv = sexp.find(circuit, "use_via")
            if uv:
                nc.use_vias = [t for t in uv[1:] if not isinstance(t, list)]
        for rule in sexp.find_all(cls_node, "rule"):
            for sub in rule[1:]:
                if not isinstance(sub, list):
                    continue
                if sub[0] == "width":
                    nc.width = _f(sub[1])
                elif sub[0] == "clearance" and len(sub) == 2:
                    nc.clearance = _f(sub[1])
                elif sub[0] in _LENGTH_RULES:
                    # unit-scale length-valued rules; consumers read floats
                    nc.rules[sub[0]] = [
                        _f(t) if not isinstance(t, list) else t for t in sub[1:]
                    ]
                else:
                    nc.rules[sub[0]] = sub[1:]
        board.classes[name] = nc
        for net_name in nets:
            if net_name in board.nets:
                board.nets[net_name].net_class = nc

    # attach net names to pads
    for net in board.nets.values():
        for pid in net.pad_ids:
            if pid in board.pads:
                board.pads[pid].net = net.name

    return board
