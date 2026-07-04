"""Verify the nogil geometry kernels against shapely — the quality guard."""

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from chaosrouter.geom_kernels import (
    point_in_poly, seg_poly_dist, seg_point_dist2, seg_seg_dist2,
)

RNG = np.random.default_rng(20260704)


def _rp(scale=100.0):
    return tuple(RNG.uniform(-scale, scale, 2))


@pytest.mark.parametrize("i", range(300))
def test_seg_point(i):
    a, b, p = _rp(), _rp(), _rp()
    got = seg_point_dist2(a[0], a[1], b[0], b[1], p[0], p[1]) ** 0.5
    exp = LineString([a, b]).distance(Point(p))
    assert got == pytest.approx(exp, abs=1e-6)


@pytest.mark.parametrize("i", range(300))
def test_seg_seg(i):
    a, b, c, d = _rp(), _rp(), _rp(), _rp()
    got = seg_seg_dist2(a[0], a[1], b[0], b[1], c[0], c[1], d[0], d[1]) ** 0.5
    exp = LineString([a, b]).distance(LineString([c, d]))
    assert got == pytest.approx(exp, abs=1e-6)


@pytest.mark.parametrize("i", range(200))
def test_seg_poly(i):
    # random convex-ish quad
    cx, cy = _rp(50)
    pts = sorted(
        [(cx + RNG.uniform(-30, 30), cy + RNG.uniform(-30, 30)) for _ in range(4)],
        key=lambda p: np.arctan2(p[1] - cy, p[0] - cx),
    )
    poly = Polygon(pts)
    if not poly.is_valid or poly.area < 1:
        return
    px = np.array([p[0] for p in pts])
    py = np.array([p[1] for p in pts])
    a, b = _rp(), _rp()
    got = seg_poly_dist(a[0], a[1], b[0], b[1], px, py)
    exp = LineString([a, b]).distance(poly)
    assert got == pytest.approx(exp, abs=1e-5)


@pytest.mark.parametrize("i", range(200))
def test_point_in_poly(i):
    cx, cy = _rp(50)
    pts = sorted(
        [(cx + RNG.uniform(-30, 30), cy + RNG.uniform(-30, 30)) for _ in range(5)],
        key=lambda p: np.arctan2(p[1] - cy, p[0] - cx),
    )
    poly = Polygon(pts)
    if not poly.is_valid or poly.area < 1:
        return
    px = np.array([p[0] for p in pts])
    py = np.array([p[1] for p in pts])
    q = _rp(60)
    got = point_in_poly(q[0], q[1], px, py)
    exp = poly.contains(Point(q))
    # boundary cases can disagree; only assert clear interior/exterior
    if poly.boundary.distance(Point(q)) > 0.5:
        assert got == exp
