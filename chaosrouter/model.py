"""Board data model. All coordinates in mils, angles in degrees CCW."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry


LAYERS = ("Top", "Bottom")


@dataclass
class PadShape:
    """One shape of a padstack on one layer."""

    layer: str  # "Top" / "Bottom"
    geometry: BaseGeometry  # shapely geometry in padstack-local coords


@dataclass
class Padstack:
    name: str
    shapes: list[PadShape] = field(default_factory=list)

    def layers(self) -> set[str]:
        return {s.layer for s in self.shapes}

    def is_through(self) -> bool:
        return len(self.layers()) > 1

    def shape_on(self, layer: str) -> BaseGeometry | None:
        for s in self.shapes:
            if s.layer == layer:
                return s.geometry
        return None


@dataclass
class Pad:
    """A physical pad instance on the board (absolute coordinates)."""

    ref: str  # component refdes, e.g. "C1"
    pin: str  # pin name, e.g. "2"
    padstack: Padstack
    x: float
    y: float
    rotation: float  # total rotation applied to padstack shape, deg CCW
    mirrored: bool  # True for back-side components (shape x-mirrored)
    net: str | None = None
    board_layers: tuple = LAYERS  # full stackup, outermost first

    @property
    def pin_id(self) -> str:
        return f"{self.ref}-{self.pin}"

    def _mirror_layer(self, layer: str) -> str:
        """Back-side placement flips the stackup: layer i <-> layer n-1-i."""
        try:
            i = self.board_layers.index(layer)
        except ValueError:
            return layer
        return self.board_layers[len(self.board_layers) - 1 - i]

    def layers(self) -> set[str]:
        if self.padstack.is_through():
            # drilled: the barrel exists on every layer of the stackup
            return set(self.board_layers)
        layer = next(iter(self.padstack.layers()))
        if self.mirrored:
            layer = self._mirror_layer(layer)
        return {layer}

    def geometry_on(self, layer: str) -> BaseGeometry | None:
        """Absolute pad copper geometry on the given board layer."""
        src_layer = self._mirror_layer(layer) if self.mirrored else layer
        geom = self.padstack.shape_on(src_layer)
        if geom is None and self.padstack.is_through():
            # inner layer not listed explicitly: the drilled barrel is still
            # there — use the largest listed shape as a conservative stand-in
            geom = max(
                (s.geometry for s in self.padstack.shapes),
                key=lambda g: g.area,
                default=None,
            )
        if geom is None:
            return None
        return transform_geom(geom, self.x, self.y, self.rotation, self.mirrored)


def transform_geom(
    geom: BaseGeometry, dx: float, dy: float, rot_deg: float, mirror: bool
) -> BaseGeometry:
    """Mirror about y-axis (if back side), rotate CCW, then translate."""
    import shapely.affinity as aff

    g = geom
    if mirror:
        g = aff.scale(g, xfact=-1, yfact=1, origin=(0, 0))
    if rot_deg % 360 != 0:
        g = aff.rotate(g, rot_deg, origin=(0, 0))
    return aff.translate(g, dx, dy)


def transform_point(px: float, py: float, dx: float, dy: float, rot_deg: float, mirror: bool):
    if mirror:
        px = -px
    r = math.radians(rot_deg)
    c, s = math.cos(r), math.sin(r)
    return (dx + px * c - py * s, dy + px * s + py * c)


@dataclass
class NetClass:
    name: str
    nets: list[str]
    width: float = 5.9055
    clearance: float = 5.9055
    use_vias: list[str] = field(default_factory=list)
    rules: dict = field(default_factory=dict)  # extra rules (diff pair etc.)


@dataclass
class Net:
    name: str
    pad_ids: list[str] = field(default_factory=list)  # "REF-PIN"
    net_class: NetClass | None = None

    @property
    def width(self) -> float:
        return self.net_class.width if self.net_class else 5.9055

    @property
    def clearance(self) -> float:
        return self.net_class.clearance if self.net_class else 5.9055


@dataclass
class Component:
    ref: str
    image_name: str
    x: float
    y: float
    side: str  # "front" / "back"
    rotation: float
    outline: list = field(default_factory=list)  # raw outline paths for viz


@dataclass
class Board:
    outline: Polygon | None = None
    layers: list[str] = field(default_factory=lambda: list(LAYERS))
    components: dict[str, Component] = field(default_factory=dict)
    pads: dict[str, Pad] = field(default_factory=dict)  # keyed "REF-PIN"
    nets: dict[str, Net] = field(default_factory=dict)
    classes: dict[str, NetClass] = field(default_factory=dict)
    padstacks: dict[str, Padstack] = field(default_factory=dict)
    via_padstacks: list[str] = field(default_factory=list)  # allowed via names
    default_width: float = 5.9055
    default_clearance: float = 5.9055

    def pads_of_net(self, net: Net) -> list[Pad]:
        return [self.pads[pid] for pid in net.pad_ids if pid in self.pads]

    def stats(self) -> str:
        n_smd = sum(1 for p in self.pads.values() if not p.padstack.is_through())
        n_th = len(self.pads) - n_smd
        conn = sum(max(0, len(n.pad_ids) - 1) for n in self.nets.values())
        return (
            f"components: {len(self.components)}  pads: {len(self.pads)} "
            f"(smd {n_smd}, through {n_th})\n"
            f"nets: {len(self.nets)}  required connections: {conn}\n"
            f"classes: {len(self.classes)}  padstacks: {len(self.padstacks)}\n"
            f"default width/clearance: {self.default_width:.3f}/{self.default_clearance:.3f} mil"
        )
