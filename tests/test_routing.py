"""End-to-end routing on synthetic boards: completion + clean DRC."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boards import simple_board

from chaosrouter import load_dsn
from chaosrouter.curves import fillet_result
from chaosrouter.drc import check, check_geometry
from chaosrouter.grid import Workspace
from chaosrouter.router import Router


def route(board):
    ws = Workspace(board, step=4.0)
    router = Router(board, ws)
    result = router.route_all()
    router.prune_open_stubs()
    router.beautify_exits()
    router.fatten_pad_entries()
    fillet_result(result, ws, board, r_target=25.0)
    return result


def test_two_layer_simple(tmp_path):
    dsn = tmp_path / "b.dsn"
    dsn.write_text(simple_board(n_pairs=4))
    board = load_dsn(str(dsn))
    result = route(board)

    assert not result.failed, f"failed edges: {result.failed}"
    violations, opens = check(board, result)
    assert violations == []
    assert opens == []
    dangling, _ = check_geometry(board, result)
    assert dangling == []


def test_um_board_routes_like_mil(tmp_path):
    dsn = tmp_path / "b.dsn"
    dsn.write_text(simple_board(unit="um", n_pairs=2))
    board = load_dsn(str(dsn))
    result = route(board)
    assert not result.failed
    violations, opens = check(board, result)
    assert violations == []
    assert opens == []


def test_walls_force_inner_layers(tmp_path):
    dsn = tmp_path / "b.dsn"
    dsn.write_text(
        simple_board(layers=("Top", "In1", "In2", "Bottom"), n_pairs=3, wall=True)
    )
    board = load_dsn(str(dsn))
    result = route(board)
    assert not result.failed
    layers_used = {t.layer for t in result.traces}
    assert layers_used & {"In1", "In2"}, "expected inner-layer routing"
    violations, opens = check(board, result)
    assert violations == []
    assert opens == []


def test_diff_pair_stays_coupled(tmp_path):
    from chaosrouter.drc import check_pairs

    dsn = tmp_path / "b.dsn"
    dsn.write_text(simple_board(n_pairs=1, diff_pair=True))
    board = load_dsn(str(dsn))
    result = route(board)
    assert not result.failed
    pairs = check_pairs(board, result)
    assert pairs, "diff pair not detected"
    _, _, pct, uncoupled = pairs[0]
    assert pct > 80.0 or uncoupled < 400.0
