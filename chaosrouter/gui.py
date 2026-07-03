"""chaosRouter GUI — dark, minimal, statistics-forward.

The board view is fully vector (crisp zoom) and animates copper LIVE as
the routing subprocess streams trace events. Routing runs as a subprocess
of this same executable (`chaosRouter route ...`), so Cancel is instant
and the GUI can never freeze.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from PySide6.QtCore import QPointF, QProcess, QSettings, Qt, QThread, Signal
from PySide6.QtGui import (
    QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame,
    QGraphicsScene, QGraphicsView, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from .version import APP_NAME, HISTORY, UPDATE_URL, __version__

# ---------------------------------------------------------------- theme
ACCENT = "#f5a623"
ACCENT2 = "#31b0d5"
GOOD = "#3fbf6f"
BAD = "#e05555"
BG = "#0b0b0d"
PANEL = "#141418"
PANEL2 = "#1b1b21"
TEXT = "#e8e8ea"
MUTED = "#8a8a92"

LAYER_COLORS = ["#f5a623", "#31b0d5", "#3fbf6f", "#e05555",
                "#8a6cf0", "#d05ce3", "#5ce3c7", "#e3b25c"]
VIA_COLOR = "#e8c34a"

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
QPushButton#cancel {{ background: #4a1a1a; color: #e0a0a0; }}
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


class BoardLoader(QThread):
    """Parse a DSN off the UI thread."""

    ok = Signal(object, str)
    fail = Signal(str)

    def __init__(self, dsn: str):
        super().__init__()
        self.dsn = dsn

    def run(self):
        try:
            from . import load_dsn

            board = load_dsn(self.dsn)
            self.ok.emit(board, board.stats())
        except Exception as e:
            self.fail.emit(str(e))


# ---------------------------------------------------------------- board view
class BoardView(QGraphicsView):
    """Vector pan/zoom board view with live copper animation."""

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene())
        self.setRenderHints(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.scale(1, -1)  # PCB y-up
        self.layer_color: dict[str, QColor] = {}
        self.net_items: dict[str, list] = {}
        self._loaded = False

    def color_for(self, layer: str) -> QColor:
        if layer not in self.layer_color:
            c = QColor(LAYER_COLORS[len(self.layer_color) % len(LAYER_COLORS)])
            self.layer_color[layer] = c
        return self.layer_color[layer]

    def load_board(self, board):
        """Static content: outline + pads."""
        sc = self.scene()
        sc.clear()
        self.net_items.clear()
        self.layer_color.clear()
        for ly in board.layers:
            self.color_for(ly)
        # outline
        try:
            ext = list(board.outline.exterior.coords)
            path = QPainterPath(QPointF(*ext[0]))
            for x, y in ext[1:]:
                path.lineTo(x, y)
            item = sc.addPath(path, QPen(QColor("#3a3a42"), 2))
            item.setZValue(0)
        except Exception:
            pass
        # pads
        for pad in board.pads.values():
            for layer in pad.layers():
                g = pad.geometry_on(layer)
                if g is None or not hasattr(g, "exterior"):
                    continue
                poly = QPolygonF([QPointF(x, y) for x, y in g.exterior.coords])
                col = QColor(self.color_for(layer))
                col.setAlphaF(0.42)
                it = sc.addPolygon(poly, QPen(Qt.NoPen), QBrush(col))
                it.setZValue(1)
        self.setSceneRect(sc.itemsBoundingRect().adjusted(-80, -80, 80, 80))
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
        self._loaded = True

    # ---- live copper -------------------------------------------------
    def add_trace(self, net: str, layer: str, pts, width: float):
        if not pts:
            return
        path = QPainterPath(QPointF(*pts[0]))
        for x, y in pts[1:]:
            path.lineTo(x, y)
        pen = QPen(self.color_for(layer), max(width, 1.0))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        it = self.scene().addPath(path, pen)
        it.setZValue(3)
        it.setOpacity(0.9)
        self.net_items.setdefault(net, []).append(it)

    def add_via(self, net: str, x: float, y: float, dia: float):
        r = dia / 2
        it = self.scene().addEllipse(
            x - r, y - r, dia, dia, QPen(QColor("#111111"), 0.8),
            QBrush(QColor(VIA_COLOR)),
        )
        it.setZValue(4)
        self.net_items.setdefault(net, []).append(it)

    def rip_net(self, net: str):
        for it in self.net_items.pop(net, []):
            self.scene().removeItem(it)

    def clear_copper(self):
        for items in self.net_items.values():
            for it in items:
                self.scene().removeItem(it)
        self.net_items.clear()

    def wheelEvent(self, ev):
        if not self._loaded:
            return
        f = 1.25 if ev.angleDelta().y() > 0 else 0.8
        self.scale(f, f)


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
        self.setMinimumSize(560, 520)
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
        self.setAcceptDrops(True)
        self.settings = QSettings("CodeMesserMaster", APP_NAME)
        self.proc: QProcess | None = None
        self.loader: BoardLoader | None = None
        self.last_stats: dict | None = None
        self._stats_path = ""

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
        sub = QLabel(f"r{__version__} · guided-chaos routing")
        sub.setObjectName("brandSub")
        pl.addWidget(brand)
        pl.addWidget(sub)
        pl.addSpacing(18)

        pl.addWidget(QLabel("Input DSN  (or drop a file anywhere)"))
        row = QHBoxLayout()
        self.dsn_edit = QLineEdit(self.settings.value("dsn", ""))
        self.dsn_edit.setPlaceholderText("pick a .dsn export ...")
        self.dsn_edit.editingFinished.connect(self._dsn_edited)
        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(36)
        btn_browse.clicked.connect(self.pick_dsn)
        row.addWidget(self.dsn_edit)
        row.addWidget(btn_browse)
        pl.addLayout(row)

        pl.addSpacing(8)
        pl.addWidget(QLabel("Output name"))
        self.out_edit = QLineEdit(self.settings.value("out", "routed"))
        pl.addWidget(self.out_edit)

        grid = QGridLayout()
        grid.addWidget(QLabel("Method"), 2, 0)
        self.method_combo = QComboBox()
        self.method_combo.addItems(["Guided-Chaos (default)", "PathFinder (experimental)"])
        self.method_combo.setCurrentIndex(
            1 if self.settings.value("method", "chaos") == "pathfinder" else 0
        )
        grid.addWidget(self.method_combo, 2, 1)
        grid.addWidget(QLabel("Grid step (mil)"), 0, 0)
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(1.0, 20.0)
        self.step_spin.setValue(float(self.settings.value("step", 4.0)))
        grid.addWidget(self.step_spin, 0, 1)
        grid.addWidget(QLabel("Fillet radius (mil)"), 1, 0)
        self.fillet_spin = QDoubleSpinBox()
        self.fillet_spin.setRange(2.0, 100.0)
        self.fillet_spin.setValue(float(self.settings.value("fillet", 25.0)))
        grid.addWidget(self.fillet_spin, 1, 1)
        pl.addSpacing(8)
        pl.addLayout(grid)
        self.strict_chk = QCheckBox("Strict class widths (no neck-down)")
        self.strict_chk.setChecked(
            str(self.settings.value("strict", "false")).lower() == "true"
        )
        self.strict_chk.setToolTip(
            "DipTrace's SES import clamps wire widths UP to the net class "
            "width (widths above class import fine). To keep neck-downs, "
            "leave this off and temporarily lower the power class widths "
            "in DipTrace to the minimum routed width before importing, "
            "then restore them. Turn this on to never route below class "
            "width instead (no DipTrace steps needed)."
        )
        pl.addSpacing(6)
        pl.addWidget(self.strict_chk)

        pl.addSpacing(16)
        self.go = QPushButton("ROUTE")
        self.go.setObjectName("go")
        self.go.clicked.connect(self.start_route)
        pl.addWidget(self.go)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel_route)
        pl.addWidget(self.cancel_btn)
        self.save_btn = QPushButton("Save result …")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save_result)
        pl.addWidget(self.save_btn)
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

        if os.path.isfile(self.dsn_edit.text().strip()):
            self.show_preview(self.dsn_edit.text().strip())

    # ---- drag & drop ---------------------------------------------------
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in ev.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".dsn"):
                self._set_dsn(path)
                return

    # ---- input ---------------------------------------------------------
    def pick_dsn(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick DSN export",
            os.path.dirname(self.dsn_edit.text()) or "",
            "Specctra DSN (*.dsn);;All files (*)",
        )
        if path:
            self._set_dsn(path)

    def _set_dsn(self, path: str):
        self.dsn_edit.setText(path)
        base = os.path.splitext(os.path.basename(path))[0]
        self.out_edit.setText(base + "_routed")
        self.show_preview(path)

    def _dsn_edited(self):
        p = self.dsn_edit.text().strip()
        if os.path.isfile(p):
            self.show_preview(p)

    def show_preview(self, path: str):
        self.status.setText("loading board ...")
        self.loader = BoardLoader(path)
        self.loader.ok.connect(self._board_loaded)
        self.loader.fail.connect(
            lambda msg: self.status.setText(f"could not load: {msg}")
        )
        self.loader.start()

    def _board_loaded(self, board, summary: str):
        self.view.load_board(board)
        self.status.setText(summary)
        self.tabs.setCurrentWidget(self.view)

    # ---- routing subprocess ---------------------------------------------
    def _route_cmd(self, dsn, out_base) -> tuple[str, list[str]]:
        args = [
            "route", dsn, "--out", out_base,
            "--step", str(self.step_spin.value()),
            "--fillet", str(self.fillet_spin.value()),
            "--stream", "--stats-json", self._stats_path,
        ]
        if self.strict_chk.isChecked():
            args.append("--strict-width")
        if self.method_combo.currentIndex() == 1:
            args += ["--method", "pathfinder"]
        if getattr(sys, "frozen", False):
            return sys.executable, args
        launcher = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "chaosRouter.py",
        )
        return sys.executable, [launcher] + args

    def start_route(self):
        dsn = self.dsn_edit.text().strip()
        if not dsn or not os.path.isfile(dsn):
            QMessageBox.warning(self, APP_NAME, "Pick a valid .dsn file first.")
            return
        import tempfile

        self.settings.setValue("dsn", dsn)
        self.settings.setValue("out", self.out_edit.text())
        self.settings.setValue("step", self.step_spin.value())
        self.settings.setValue("fillet", self.fillet_spin.value())
        self.settings.setValue("strict", self.strict_chk.isChecked())
        self.settings.setValue(
            "method",
            "pathfinder" if self.method_combo.currentIndex() == 1 else "chaos",
        )
        out_base = os.path.join(
            os.path.dirname(dsn), self.out_edit.text().strip() or "routed"
        )
        self._stats_path = os.path.join(tempfile.gettempdir(), "chaosrouter_stats.json")
        self.view.clear_copper()
        self.log.clear()
        self.go.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.save_btn.setEnabled(False)
        self.status.setText("routing ... watch the board")
        self.tabs.setCurrentWidget(self.view)

        prog, args = self._route_cmd(dsn, out_base)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._proc_output)
        self.proc.finished.connect(self._proc_done)
        self.proc.start(prog, args)

    def cancel_route(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.status.setText("cancelled")
            self.log.appendPlainText("\n[cancelled by user]")

    def _proc_output(self):
        data = bytes(self.proc.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        for line in data.splitlines():
            if line.startswith("@T|"):
                try:
                    _, net, layer, width, pts = line.split("|", 4)
                    coords = [
                        tuple(map(float, p.split(","))) for p in pts.split(";")
                    ]
                    self.view.add_trace(net, layer, coords, float(width))
                except ValueError:
                    pass
            elif line.startswith("@V|"):
                try:
                    _, net, x, y, dia = line.split("|", 4)
                    self.view.add_via(net, float(x), float(y), float(dia))
                except ValueError:
                    pass
            elif line.startswith("@R|"):
                self.view.rip_net(line[3:])
            elif line.strip():
                self.log.appendPlainText(line)

    def _proc_done(self, code, _status):
        self.go.setEnabled(True)
        self.cancel_btn.setVisible(False)
        stats = None
        if os.path.isfile(self._stats_path):
            try:
                with open(self._stats_path, encoding="utf-8") as fh:
                    stats = json.load(fh)
            except (OSError, json.JSONDecodeError):
                stats = None
        if stats is None:
            if self.status.text() != "cancelled":
                self.status.setText(f"routing exited (code {code}) — see log")
            return
        self.last_stats = stats
        self.save_btn.setEnabled(True)
        r = stats["routing"]
        self.status.setText(
            f"done: {r['routed']}/{r['total']} in {r['seconds']:.0f}s — "
            f"session written to {os.path.basename(stats['ses'])}"
        )
        # redraw exact final copper (style passes reshape everything)
        self.view.clear_copper()
        geo = stats.get("geometry") or {}
        for net, layer, coords, width in geo.get("traces", []):
            self.view.add_trace(net, layer, [tuple(c) for c in coords], width)
        for net, x, y, dia in geo.get("vias", []):
            self.view.add_via(net, x, y, dia)
        self.populate_stats(stats)

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
            small("grid + route + style + DRC"),
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
            ok = pair["coupled_pct"] > 90 or pair["uncoupled_mil"] < 100
            bot.addWidget(card(
                f"pair {pair['p']}",
                big("OK" if ok else f"{pair['coupled_pct']}%",
                    GOOD if ok else ACCENT),
                small(
                    f"{pair['coupled_pct']}% coupled · "
                    f"{pair['uncoupled_mil']:.0f} mil uncoupled"
                ),
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

    # ---- save / updates -------------------------------------------------
    def save_result(self):
        if not self.last_stats:
            return
        import shutil

        default = os.path.abspath(self.last_stats["ses"])
        path, _ = QFileDialog.getSaveFileName(
            self, "Save routed session", default,
            "Specctra session (*.ses);;All files (*)",
        )
        if not path:
            return
        shutil.copyfile(self.last_stats["ses"], path)
        png_target = os.path.splitext(path)[0] + ".png"
        try:
            shutil.copyfile(self.last_stats["png"], png_target)
        except OSError:
            png_target = None
        self.status.setText(
            f"saved {os.path.basename(path)}"
            + (f" + {os.path.basename(png_target)}" if png_target else "")
        )

    def check_updates(self):
        try:
            req = urllib.request.Request(
                UPDATE_URL, headers={"Accept": "application/vnd.github+json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                info = json.load(resp)
            latest = str(
                info.get("tag_name") or info.get("version") or "?"
            ).lstrip("v")
            if latest != __version__ and latest != "?":
                QMessageBox.information(
                    self, APP_NAME,
                    f"Version {latest} is available (you run {__version__}).\n"
                    f"{info.get('html_url', info.get('url', ''))}",
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
    from PySide6.QtGui import QIcon

    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"CodeMesserMaster.chaosRouter.{__version__}"
        )

    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setApplicationName(APP_NAME)
    icon_path = os.path.join(os.path.dirname(__file__), "assets", "icon_512.png")
    icon = QIcon(icon_path) if os.path.isfile(icon_path) else None
    if icon:
        app.setWindowIcon(icon)
    win = Main()
    if icon:
        win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
