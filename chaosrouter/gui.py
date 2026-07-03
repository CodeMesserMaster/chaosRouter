"""chaosRouter GUI — dark, minimal, statistics-forward.

Run with:  python -m chaosrouter.gui   (or the chaosRouter launcher)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QGraphicsScene,
    QGraphicsView, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QTabWidget, QVBoxLayout, QWidget,
)

from .version import APP_NAME, HISTORY, UPDATE_URL, __version__

# ---------------------------------------------------------------- theme
ACCENT = "#f5a623"      # Top-layer orange
ACCENT2 = "#31b0d5"     # cool cyan
GOOD = "#3fbf6f"
BAD = "#e05555"
BG = "#0b0b0d"
PANEL = "#141418"
PANEL2 = "#1b1b21"
TEXT = "#e8e8ea"
MUTED = "#8a8a92"

QSS = f"""
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; font-size: 13px; }}
QFrame#panel {{ background: {PANEL}; border-radius: 10px; }}
QFrame#card {{ background: {PANEL2}; border-radius: 10px; }}
QLabel#brand {{ color: {ACCENT}; font-size: 26px; font-weight: 800; }}
QLabel#brandSub {{ color: {MUTED}; font-size: 12px; }}
QLabel#big {{ font-size: 30px; font-weight: 700; }}
QLabel#cardTitle {{ color: {MUTED}; font-size: 11px; letter-spacing: 1px; }}
QLabel#muted {{ color: {MUTED}; }}
QPushButton {{
    background: {PANEL2}; border: 1px solid #26262e; border-radius: 8px;
    padding: 8px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton#go {{
    background: {ACCENT}; color: #14100a; font-weight: 800; font-size: 15px;
    padding: 12px; border: none;
}}
QPushButton#go:disabled {{ background: #4a3a1a; color: #7a6a4a; }}
QLineEdit, QDoubleSpinBox {{
    background: {PANEL2}; border: 1px solid #26262e; border-radius: 6px;
    padding: 6px;
}}
QPlainTextEdit {{
    background: #0e0e11; border: none; border-radius: 8px;
    color: #b9b9c2; font-family: Consolas, Menlo, monospace; font-size: 12px;
}}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 8px 18px;
    border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QScrollArea {{ border: none; }}
QGraphicsView {{ background: {BG}; border: none; border-radius: 10px; }}
"""


# ---------------------------------------------------------------- worker
class PreviewWorker(QThread):
    """Render the unrouted board (pads + outline + ratsnest) on file pick."""

    ok = Signal(str, str)  # png path, board summary
    fail = Signal(str)

    def __init__(self, dsn: str):
        super().__init__()
        self.dsn = dsn

    def run(self):
        try:
            import tempfile

            from . import load_dsn
            from .viz import draw_board

            board = load_dsn(self.dsn)
            png = os.path.join(tempfile.gettempdir(), "chaosrouter_preview.png")
            draw_board(
                board, png,
                title=f"{os.path.basename(self.dsn)} — unrouted",
            )
            self.ok.emit(png, board.stats())
        except Exception as e:
            self.fail.emit(str(e))


class RouteWorker(QThread):
    line = Signal(str)
    done = Signal(dict)
    fail = Signal(str)

    def __init__(self, dsn, out_base, step, fillet_r):
        super().__init__()
        self.args = (dsn, out_base, step, fillet_r)

    def run(self):
        try:
            from .pipeline import run_pipeline

            dsn, out_base, step, fillet_r = self.args
            stats = run_pipeline(
                dsn, out_base=out_base, step=step, fillet_r=fillet_r,
                progress=self.line.emit,
            )
            self.done.emit(stats)
        except Exception as e:  # surface, never crash the GUI
            import traceback

            self.fail.emit(f"{e}\n{traceback.format_exc()}")


# ---------------------------------------------------------------- widgets
class BoardView(QGraphicsView):
    """Pan/zoom preview of the rendered board."""

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene())
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self._has_image = False

    def set_image(self, path: str):
        self.scene().clear()
        pm = QPixmap(path)
        self.scene().addPixmap(pm)
        self.setSceneRect(pm.rect())
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
        self._has_image = True

    def wheelEvent(self, ev):
        if not self._has_image:
            return
        f = 1.25 if ev.angleDelta().y() > 0 else 0.8
        self.scale(f, f)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)


def card(title: str, value_label: QLabel, sub: QLabel | None = None) -> QFrame:
    fr = QFrame()
    fr.setObjectName("card")
    lay = QVBoxLayout(fr)
    lay.setContentsMargins(14, 10, 14, 10)
    t = QLabel(title.upper())
    t.setObjectName("cardTitle")
    lay.addWidget(t)
    lay.addWidget(value_label)
    if sub is not None:
        lay.addWidget(sub)
    return fr


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumSize(560, 480)
        lay = QVBoxLayout(self)
        head = QLabel(APP_NAME)
        head.setObjectName("brand")
        lay.addWidget(head)
        lay.addWidget(QLabel(f"version {__version__} — curved-trace autorouter "
                             f"for any DSN-exporting CAD"))
        tech = QLabel(
            "<b>Guided-Chaos Routing</b>: optimistic grid search judged by "
            "exact geometry (true-circle clearance math), an escalating "
            "ladder of controlled perturbation — rip-up with blocker "
            "attribution, Monte-Carlo shaking, and a surgical eviction "
            "endgame — plus fully verified curvilinear styling: tangent-arc "
            "fillets, teardrop entries, graded 0.1 mm neck-downs."
        )
        tech.setWordWrap(True)
        tech.setStyleSheet(f"color: {MUTED}; padding: 6px 0;")
        lay.addWidget(tech)
        hist = QPlainTextEdit()
        hist.setReadOnly(True)
        txt = []
        for ver, date, notes in HISTORY:
            txt.append(f"v{ver}  ({date})")
            for n in notes:
                txt.append(f"   • {n}")
            txt.append("")
        hist.setPlainText("\n".join(txt))
        lay.addWidget(QLabel("Version history:"))
        lay.addWidget(hist)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        lay.addWidget(close, alignment=Qt.AlignRight)


# ---------------------------------------------------------------- window
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  ·  r{__version__}")
        self.resize(1480, 920)
        self.worker: RouteWorker | None = None

        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)

        # ---- left: control panel ------------------------------------
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setFixedWidth(320)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(18, 18, 18, 18)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        sub = QLabel(f"r{__version__} · curves only · exact DRC")
        sub.setObjectName("brandSub")
        pl.addWidget(brand)
        pl.addWidget(sub)
        pl.addSpacing(18)

        pl.addWidget(QLabel("Input DSN"))
        row = QHBoxLayout()
        self.dsn_edit = QLineEdit()
        self.dsn_edit.setPlaceholderText("pick a .dsn export ...")
        self.dsn_edit.editingFinished.connect(
            lambda: os.path.isfile(self.dsn_edit.text().strip())
            and self.show_preview(self.dsn_edit.text().strip())
        )
        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(36)
        btn_browse.clicked.connect(self.pick_dsn)
        row.addWidget(self.dsn_edit)
        row.addWidget(btn_browse)
        pl.addLayout(row)

        pl.addSpacing(8)
        pl.addWidget(QLabel("Output name"))
        self.out_edit = QLineEdit("routed")
        pl.addWidget(self.out_edit)

        grid = QGridLayout()
        grid.addWidget(QLabel("Grid step (mil)"), 0, 0)
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(1.0, 20.0)
        self.step_spin.setValue(4.0)
        grid.addWidget(self.step_spin, 0, 1)
        grid.addWidget(QLabel("Fillet radius (mil)"), 1, 0)
        self.fillet_spin = QDoubleSpinBox()
        self.fillet_spin.setRange(2.0, 100.0)
        self.fillet_spin.setValue(25.0)
        grid.addWidget(self.fillet_spin, 1, 1)
        pl.addSpacing(8)
        pl.addLayout(grid)

        pl.addSpacing(16)
        self.go = QPushButton("ROUTE")
        self.go.setObjectName("go")
        self.go.clicked.connect(self.start_route)
        pl.addWidget(self.go)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        pl.addWidget(self.status)

        pl.addStretch(1)
        row2 = QHBoxLayout()
        btn_upd = QPushButton("Check for updates")
        btn_upd.clicked.connect(self.check_updates)
        btn_about = QPushButton("About")
        btn_about.clicked.connect(lambda: AboutDialog(self).exec())
        row2.addWidget(btn_upd)
        row2.addWidget(btn_about)
        pl.addLayout(row2)
        outer.addWidget(panel)

        # ---- right: tabs ---------------------------------------------
        self.tabs = QTabWidget()
        self.view = BoardView()
        self.tabs.addTab(self.view, "Board")

        self.stats_scroll = QScrollArea()
        self.stats_scroll.setWidgetResizable(True)
        self.stats_body = QWidget()
        self.stats_lay = QVBoxLayout(self.stats_body)
        self.stats_lay.addStretch(1)
        self.stats_scroll.setWidget(self.stats_body)
        self.tabs.addTab(self.stats_scroll, "Statistics")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, "Log")
        outer.addWidget(self.tabs, 1)

    # ---- actions -----------------------------------------------------
    def pick_dsn(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick DSN export", "", "Specctra DSN (*.dsn);;All files (*)"
        )
        if path:
            self.dsn_edit.setText(path)
            base = os.path.splitext(os.path.basename(path))[0]
            self.out_edit.setText(base + "_routed")
            self.show_preview(path)

    def show_preview(self, path: str):
        """Show the unrouted board in the Board tab as soon as it's picked."""
        self.status.setText("loading board ...")
        self.preview = PreviewWorker(path)
        self.preview.ok.connect(self._preview_ready)
        self.preview.fail.connect(
            lambda msg: self.status.setText(f"could not preview: {msg}")
        )
        self.preview.start()

    def _preview_ready(self, png: str, summary: str):
        self.view.set_image(png)
        self.status.setText(summary)
        self.tabs.setCurrentWidget(self.view)

    def start_route(self):
        dsn = self.dsn_edit.text().strip()
        if not dsn or not os.path.isfile(dsn):
            QMessageBox.warning(self, APP_NAME, "Pick a valid .dsn file first.")
            return
        out_base = os.path.join(os.path.dirname(dsn), self.out_edit.text().strip() or "routed")
        self.go.setEnabled(False)
        self.status.setText("routing ... (this can take several minutes)")
        self.log.clear()
        self.tabs.setCurrentWidget(self.log)
        self.worker = RouteWorker(
            dsn, out_base, self.step_spin.value(), self.fillet_spin.value()
        )
        self.worker.line.connect(self.log.appendPlainText)
        self.worker.done.connect(self.finished)
        self.worker.fail.connect(self.failed)
        self.worker.start()

    def failed(self, msg: str):
        self.go.setEnabled(True)
        self.status.setText("failed — see log")
        self.log.appendPlainText("\nERROR:\n" + msg)

    def finished(self, stats: dict):
        self.go.setEnabled(True)
        r = stats["routing"]
        q = stats["quality"]
        self.status.setText(
            f"done: {r['routed']}/{r['total']} in {r['seconds']:.0f}s"
        )
        self.view.set_image(stats["png"])
        self.populate_stats(stats)
        self.tabs.setCurrentWidget(self.view)

    # ---- statistics tab ----------------------------------------------
    def populate_stats(self, s: dict):
        while self.stats_lay.count():
            item = self.stats_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        r, q, b = s["routing"], s["quality"], s["board"]

        def big(text, color=TEXT):
            lab = QLabel(text)
            lab.setObjectName("big")
            lab.setStyleSheet(f"color: {color};")
            return lab

        def small(text):
            lab = QLabel(text)
            lab.setObjectName("muted")
            lab.setWordWrap(True)
            return lab

        top = QHBoxLayout()
        pct = r["percent"]
        top.addWidget(card(
            "completion", big(f"{pct:.1f}%", GOOD if pct >= 100 else ACCENT),
            small(f"{r['routed']} of {r['total']} connections"),
        ))
        viol = len(q["violations"])
        opens = len(q["open_nets"])
        top.addWidget(card(
            "drc", big(str(viol), GOOD if viol == 0 else BAD),
            small(f"clearance violations · {opens} open nets"),
        ))
        top.addWidget(card(
            "vias", big(str(r["vias"])),
            small(" · ".join(f"{k} mil ×{v}" for k, v in r["vias_by_size"].items())),
        ))
        top.addWidget(card(
            "time", big(f"{r['seconds']:.0f}s"),
            small(f"grid + route + style + DRC"),
        ))
        w = QWidget()
        w.setLayout(top)
        self.stats_lay.addWidget(w)

        mid = QHBoxLayout()
        mid.addWidget(card(
            "board", big(f"{b['size_mm'][0]} × {b['size_mm'][1]} mm"),
            small(
                f"{b['components']} components · {b['pads']} pads · "
                f"{b['nets']} nets · {len(b['layers'])} layers"
            ),
        ))
        lens = " · ".join(
            f"{k}: {v/1000:.1f}k mil" for k, v in r["trace_len_by_layer_mil"].items()
        )
        mid.addWidget(card(
            "copper", big(f"{sum(r['trace_len_by_layer_mil'].values())/1000:.1f}k mil"),
            small(lens + f" · widths {r['width_min']}–{r['width_max']} mil"),
        ))
        w2 = QWidget()
        w2.setLayout(mid)
        self.stats_lay.addWidget(w2)

        bot = QHBoxLayout()
        for pair in q["pairs"]:
            bot.addWidget(card(
                f"pair {pair['p']}",
                big(f"{pair['coupled_pct']}%",
                    GOOD if pair["coupled_pct"] > 90 or pair["uncoupled_mil"] < 100
                    else ACCENT),
                small(f"coupled · {pair['uncoupled_mil']:.0f} mil uncoupled"),
            ))
        corners = len(q["sharp_corners"])
        bot.addWidget(card(
            "style", big(str(corners), GOOD if corners == 0 else ACCENT),
            small(f"sharp corners >30° · {len(q['dangling'])} dangling ends"),
        ))
        w3 = QWidget()
        w3.setLayout(bot)
        self.stats_lay.addWidget(w3)

        if r["failed"]:
            txt = "\n".join(f"{n}:  {a}  ↔  {b_}" for n, a, b_ in r["failed"])
            box = QPlainTextEdit()
            box.setReadOnly(True)
            box.setPlainText(txt)
            box.setMaximumHeight(140)
            self.stats_lay.addWidget(QLabel("Unrouted connections:"))
            self.stats_lay.addWidget(box)
        self.stats_lay.addStretch(1)

    # ---- update check --------------------------------------------------
    def check_updates(self):
        try:
            with urllib.request.urlopen(UPDATE_URL, timeout=4) as resp:
                info = json.load(resp)
            latest = info.get("version", "?")
            if latest != __version__:
                QMessageBox.information(
                    self, APP_NAME,
                    f"Version {latest} is available (you run {__version__}).\n"
                    f"{info.get('url', '')}",
                )
            else:
                QMessageBox.information(
                    self, APP_NAME, f"{APP_NAME} {__version__} is up to date."
                )
        except Exception:
            QMessageBox.information(
                self, APP_NAME,
                f"No update information available right now.\n"
                f"(update server not reachable — you run {__version__})",
            )


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setApplicationName(APP_NAME)
    win = Main()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
