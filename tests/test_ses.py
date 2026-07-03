"""SES output: parses as an s-expression and contains every routed net."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from boards import simple_board

from chaosrouter import load_dsn, sexp
from chaosrouter.grid import Workspace
from chaosrouter.router import Router
from chaosrouter.ses import write_ses


def test_ses_roundtrip(tmp_path):
    dsn = tmp_path / "b.dsn"
    dsn.write_text(simple_board(n_pairs=3))
    board = load_dsn(str(dsn))
    ws = Workspace(board, step=4.0)
    router = Router(board, ws)
    result = router.route_all()
    assert not result.failed

    ses = tmp_path / "b.ses"
    write_ses(str(ses), str(dsn), board, result)
    text = ses.read_text()
    root = sexp.parse(text)
    assert str(root[0]).lower() in ("session", "ses")

    routed_nets = {t.net for t in result.traces}
    for net in routed_nets:
        assert net in text, f"net {net} missing from SES"
