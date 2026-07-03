"""Units: the same board expressed in mil / um / mm must parse identically."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boards import simple_board

from chaosrouter import load_dsn


@pytest.mark.parametrize("unit", ["mil", "um", "mm"])
def test_units_normalize_to_mil(tmp_path, unit):
    dsn = tmp_path / f"b_{unit}.dsn"
    dsn.write_text(simple_board(unit=unit))
    board = load_dsn(str(dsn))

    minx, miny, maxx, maxy = board.outline.bounds
    assert maxx - minx == pytest.approx(1000.0, rel=1e-4)  # mil
    assert maxy - miny == pytest.approx(800.0, rel=1e-4)

    assert board.default_width == pytest.approx(6.0, rel=1e-4)
    assert board.default_clearance == pytest.approx(6.0, rel=1e-4)

    p = board.pads["R1-2"]
    assert p.x == pytest.approx(-360.0, rel=1e-4)
    g = p.geometry_on("Top")
    b = g.bounds
    assert b[2] - b[0] == pytest.approx(30.0, rel=1e-4)


def test_lowercase_pcb_root_accepted(tmp_path):
    dsn = tmp_path / "b.dsn"
    dsn.write_text(simple_board())  # factory emits lowercase (pcb ...)
    board = load_dsn(str(dsn))
    assert len(board.nets) >= 4


def test_length_rules_scaled(tmp_path):
    dsn = tmp_path / "b.dsn"
    dsn.write_text(simple_board(unit="um", diff_pair=True))
    board = load_dsn(str(dsn))
    cls = board.classes["PAIRS"]
    assert float(cls.rules["max_uncoupled_length"][0]) == pytest.approx(400.0, rel=1e-4)
    assert float(cls.rules["edge_primary_gap"][0]) == pytest.approx(6.0, rel=1e-4)
