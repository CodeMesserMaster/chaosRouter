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
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
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
OUTLINE_COLOR = "#ff6ea6"  # bright neon pink board outline

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
QComboBox {{
    background: {PANEL2}; color: {TEXT}; border: 1px solid #34343e;
    border-radius: 6px; padding: 6px 10px;
}}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    width: 0; height: 0; margin-right: 8px;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 6px solid {ACCENT};
}}
QComboBox QAbstractItemView {{
    background: {PANEL2}; color: {TEXT};
    border: 1px solid {ACCENT}; border-radius: 6px; outline: none;
    padding: 4px;
    selection-background-color: {ACCENT}; selection-color: #14100a;
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
QStatusBar {{ background: {BG}; border: none; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{
    background: {PANEL2}; border: 1px solid #26262e; border-radius: 8px;
    height: 16px; text-align: center; color: {MUTED}; font-size: 10px;
}}
QProgressBar::chunk {{
    border-radius: 7px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {ACCENT2}, stop:1 {ACCENT});
}}
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
    """Pan/zoom board view with a live, glowing, breathing copper animation
    — smooth bezier traces with layered glow that fade in as they route and
    pulse softly, like old bezier screensavers."""

    GLOW = True  # glowing animated rendering (vs flat lines)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene())
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.scale(1, -1)  # PCB y-up
        self.layer_color: dict[str, QColor] = {}
        self.net_items: dict[str, list] = {}
        self._loaded = False
        # animation state
        self._glow_all = []          # (item, base_opacity, birth_frame)
        self._fading = []            # [ [group, frame, target], ... ]
        self._phase = 0.0
        self._frame = 0              # global frame counter for glow ageing
        self._settling = False       # final pass: fade all glow to nothing
        self._settle_f = 0
        self._final_mode = False     # after @CLEAR: draw clean, no glow
        self._all_traces = []        # (net, layer, coords, width) — for the
        self._all_vias = []          # (net, x, y, dia)  clean finalize redraw
        from PySide6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)        # ~60 fps

    def color_for(self, layer: str) -> QColor:
        if layer not in self.layer_color:
            c = QColor(LAYER_COLORS[len(self.layer_color) % len(LAYER_COLORS)])
            self.layer_color[layer] = c
        return self.layer_color[layer]

    @staticmethod
    def _bezier(pts):
        """Catmull-Rom smoothed cubic-bezier path through the points."""
        path = QPainterPath(QPointF(*pts[0]))
        n = len(pts)
        if n < 3:
            for p in pts[1:]:
                path.lineTo(p[0], p[1])
            return path
        import math
        for i in range(1, n):
            p0 = pts[i - 2] if i >= 2 else pts[i - 1]
            p1 = pts[i - 1]
            p2 = pts[i]
            p3 = pts[i + 1] if i + 1 < n else pts[i]
            seg = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            # clamp each control handle to <= 1/3 of the segment length so the
            # curve never bulges past the segment (which read as a false cross
            # on sharp diff-pair turns)
            def handle(dx, dy, seg=seg):
                h = math.hypot(dx, dy) / 6.0
                cap = seg / 3.0
                if h > cap and h > 1e-9:
                    s = cap / h
                    return dx / 6.0 * s, dy / 6.0 * s
                return dx / 6.0, dy / 6.0
            h1x, h1y = handle(p2[0] - p0[0], p2[1] - p0[1])
            h2x, h2y = handle(p3[0] - p1[0], p3[1] - p1[1])
            path.cubicTo(p1[0] + h1x, p1[1] + h1y,
                         p2[0] - h2x, p2[1] - h2y, p2[0], p2[1])
        return path

    GLOW_FADE = 80.0  # frames (~1.3s @60fps) for a trace's glow to fade out

    def _tick(self):
        # Each trace's glow fades out over GLOW_FADE frames from its birth, so
        # the glow TRAILS the routing front like a wave and never accumulates
        # into a board-wide smear; once faded the glow layer is deleted and
        # the clean bright core remains.
        self._frame += 1
        f = self._frame
        fade = self.GLOW_FADE
        live = []
        for it, base, birth in self._glow_all:
            try:
                if it.scene() is None:
                    continue
                age = f - birth
                if age >= fade:
                    self.scene().removeItem(it)
                    continue
                # ease-out fade (bright at birth, gently to zero)
                t = 1.0 - age / fade
                it.setOpacity(base * t * t)
                live.append((it, base, birth))
            except RuntimeError:
                continue
        self._glow_all = live
        still = []
        for entry in self._fading:
            grp, f, target = entry
            try:
                if grp.scene() is None:
                    continue
                f += 1
                t = min(1.0, f / 14.0)
                grp.setOpacity(target * t * t * (3.0 - 2.0 * t))  # smoothstep
            except RuntimeError:
                continue
            if f < 14:
                entry[1] = f
                still.append(entry)
        self._fading = still

    def finalize(self):
        """Routing finished: wipe ALL copper from the scene and REDRAW every
        trace/via from scratch as clean, glow-free geometry. A full clean
        redraw cannot leave any glow behind — no dependence on @CLEAR, glow
        tracking, or fade timing."""
        self._fading = []
        self._settling = False
        self._glow_all = []
        # remove every copper item currently on the scene
        for items in self.net_items.values():
            for it in items:
                try:
                    self.scene().removeItem(it)
                except RuntimeError:
                    pass
        self.net_items.clear()
        # belt: drop any stray semi-transparent item too
        for it in list(self.scene().items()):
            try:
                if 0.0 < it.opacity() < 0.6:
                    self.scene().removeItem(it)
            except RuntimeError:
                pass
        # redraw all traces CLEAN (exact geometry, solid, no glow)
        for net, layer, pts, width in self._all_traces:
            if len(pts) < 2:
                continue
            path = QPainterPath(QPointF(*pts[0]))
            for x, y in pts[1:]:
                path.lineTo(x, y)
            pen = QPen(QColor(self.color_for(layer)).lighter(135), max(width, 1.0))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            it = self.scene().addPath(path, pen)
            it.setZValue(3)
            it.setOpacity(0.97)
            self.net_items.setdefault(net, []).append(it)
        for net, x, y, dia in self._all_vias:
            r = dia / 2
            it = self.scene().addEllipse(
                x - r, y - r, dia, dia, QPen(QColor("#111111"), 0.8),
                QBrush(QColor(VIA_COLOR)),
            )
            it.setZValue(4)
            self.net_items.setdefault(net, []).append(it)
        self.viewport().update()
        return len(self._all_traces)

    def draw_unrouted(self, edges):
        """Show failed connections as bright red ratsnest lines (pad to pad)
        so the user can see WHERE routing failed and adjust placement."""
        self.clear_unrouted()
        red = QColor("#ff2d3c")
        for x1, y1, x2, y2, _net in edges:
            path = QPainterPath(QPointF(x1, y1))
            path.lineTo(x2, y2)
            pen = QPen(red, 2.0)
            pen.setCapStyle(Qt.RoundCap)
            it = self.scene().addPath(path, pen)
            it.setZValue(20)          # on top of everything
            it.setOpacity(0.95)
            self._unrouted_items.append(it)
            for (mx, my) in ((x1, y1), (x2, y2)):
                d = self.scene().addEllipse(
                    mx - 6, my - 6, 12, 12, QPen(Qt.NoPen), QBrush(red)
                )
                d.setZValue(20)
                self._unrouted_items.append(d)
        self.viewport().update()

    def clear_unrouted(self):
        for it in getattr(self, "_unrouted_items", []):
            try:
                self.scene().removeItem(it)
            except RuntimeError:
                pass
        self._unrouted_items = []

    def load_board(self, board):
        """Static content: outline + pads."""
        sc = self.scene()
        sc.clear()
        self.net_items.clear()
        self.layer_color.clear()
        # a fresh board must never carry a previous board's stored copper
        self._all_traces = []
        self._all_vias = []
        self._unrouted_items = []
        self._final_mode = False
        self._glow_all = []
        self._fading = []
        for ly in board.layers:
            self.color_for(ly)
        # outline
        try:
            ext = list(board.outline.exterior.coords)
            path = QPainterPath(QPointF(*ext[0]))
            for x, y in ext[1:]:
                path.lineTo(x, y)
            item = sc.addPath(path, QPen(QColor(OUTLINE_COLOR), 2))
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
        self._all_traces.append((net, layer, list(pts), width))
        if self._final_mode:
            # collect only — the single clean redraw happens at finalize
            return
        if not self.GLOW:
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
            return
        from PySide6.QtWidgets import QGraphicsItemGroup

        col = self.color_for(layer)
        bright = QColor(col).lighter(150)
        path = self._bezier(pts)
        w = max(width, 1.2)
        grp = QGraphicsItemGroup()
        grp.setOpacity(0.0)
        # glow halo (mult>1) fades out over GLOW_FADE frames from birth so it
        # trails the routing front; the mult==1 core is the permanent clean
        # trace. Widest halo 3.2x keeps the bloom local.
        from PySide6.QtWidgets import QGraphicsPathItem
        for mult, alpha, c in (
            (3.2, 0.10, col),
            (1.9, 0.22, col),
            (1.3, 0.45, bright),
            (1.0, 0.97, bright),
        ):
            pen = QPen(QColor(c), w * mult)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            pit = QGraphicsPathItem(path)
            pit.setPen(pen)
            pit.setOpacity(alpha)
            grp.addToGroup(pit)
            if mult > 1.0:  # glow layer — ages out, leaving the core
                self._glow_all.append((pit, alpha, self._frame))
        grp.setZValue(3)
        self.scene().addItem(grp)
        self._fading.append([grp, 0, 1.0])
        self.net_items.setdefault(net, []).append(grp)

    def add_via(self, net: str, x: float, y: float, dia: float):
        self._all_vias.append((net, x, y, dia))
        if self._final_mode:
            return  # collect only — drawn once at finalize
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
        self._all_traces = [t for t in self._all_traces if t[0] != net]
        self._all_vias = [v for v in self._all_vias if v[0] != net]

    def clear_copper(self):
        for items in self.net_items.values():
            for it in items:
                self.scene().removeItem(it)
        self.net_items.clear()
        self._glow_all = []
        self._fading = []
        self._settling = False
        self._final_mode = False
        self._all_traces = []
        self._all_vias = []
        self._unrouted_items = []

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

        # progress bar along the bottom (fills as connections route)
        self.progress = QProgressBar()
        self.progress.setObjectName("prog")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(16)
        self.progress.setTextVisible(True)
        self.progress.setFormat("")
        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        sb.addPermanentWidget(self.progress, 1)
        self.setStatusBar(sb)

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
        self.method_combo.addItems([
            "Guided-Chaos (default)",
            "PathFinder (experimental)",
            "Manhattan (structured, dense boards)",
            "Manhattan + Fanout (experimental)",
        ])
        self._methods = ["chaos", "pathfinder", "manhattan", "manhattan-fanout"]
        saved = self.settings.value("method", "chaos")
        self.method_combo.setCurrentIndex(
            self._methods.index(saved) if saved in self._methods else 0
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
        self.draft_chk = QCheckBox("Draft (fast problem map)")
        self.draft_chk.setChecked(
            str(self.settings.value("draft", "false")).lower() == "true"
        )
        self.draft_chk.setToolTip(
            "Draft mode: first pass + one rip-up round, skipping the cleanup "
            "tail (~4x faster). Use it to iterate placement — route, see the "
            "red ratsnest, move parts, reroute — then turn off for the final "
            "full route."
        )
        pl.addSpacing(4)
        pl.addWidget(self.draft_chk)

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
        if self.draft_chk.isChecked():
            args.append("--draft")
        method = self._methods[self.method_combo.currentIndex()]
        if method != "chaos":
            args += ["--method", method]
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
        self.settings.setValue("draft", self.draft_chk.isChecked())
        self.settings.setValue(
            "method", self._methods[self.method_combo.currentIndex()]
        )
        out_base = os.path.join(
            os.path.dirname(dsn), self.out_edit.text().strip() or "routed"
        )
        self._stats_path = os.path.join(tempfile.gettempdir(), "chaosrouter_stats.json")
        self._routing_dsn = dsn
        # delete any stale stats from a previous route: if THIS route is
        # cancelled it must not read the old board's stats and redraw it
        # (the "previous board's routing overlaid on the new one" bug)
        try:
            os.remove(self._stats_path)
        except OSError:
            pass
        self.view.clear_copper()
        self._outbuf = ""
        self.log.clear()
        self.go.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.save_btn.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setFormat("routing ...")
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
        # QProcess delivers arbitrary chunks — a line (e.g. @CLEAR) can be
        # split across two reads. Buffer the trailing partial line so we only
        # ever act on COMPLETE lines; otherwise @CLEAR gets missed and the raw
        # glowing route is never replaced by the clean styled result.
        buf = getattr(self, "_outbuf", "") + data
        parts = buf.split("\n")
        self._outbuf = parts.pop()  # incomplete tail kept for next read
        for line in parts:
            line = line.rstrip("\r")
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
            elif line.startswith("@CLEAR"):
                # the final styled geometry (teardrops + fillets) follows.
                # Collect it SILENTLY (no clear, no incremental draw) — the
                # single clean swap happens once at finalize, so there is no
                # end-of-route flash/redraw on top of the live view.
                self.view._final_mode = True
                self.view._all_traces = []
                self.view._all_vias = []
                self.log.appendPlainText("· styled result received")
            elif "routing done" in line:
                # end of the routing phase — belt to @CLEAR: from here on NO
                # trace is ever drawn with glow, so the styled re-stream that
                # follows can never produce an end-of-route glow burst
                self.view._final_mode = True
                self.log.appendPlainText(line)
            elif line.startswith("@P|"):
                try:
                    _, pct, routed, failed = line.split("|", 3)
                    pct = max(0, min(100, round(float(pct))))
                    self.progress.setValue(pct)
                    self.progress.setFormat(
                        f"{routed} routed · {failed} left  ·  {pct}%"
                    )
                except ValueError:
                    pass
            elif line.strip():
                self.log.appendPlainText(line)

    def _proc_done(self, code, _status):
        self.go.setEnabled(True)
        self.cancel_btn.setVisible(False)
        # Read the authoritative final stats FIRST, before touching the view.
        stats = None
        if os.path.isfile(self._stats_path):
            try:
                with open(self._stats_path, encoding="utf-8") as fh:
                    stats = json.load(fh)
            except (OSError, json.JSONDecodeError):
                stats = None
        # reject a stats file that belongs to a DIFFERENT board (stale from a
        # previous route) — never redraw the wrong board's copper
        if stats is not None:
            sdsn = stats.get("dsn")
            want = getattr(self, "_routing_dsn", None)
            if sdsn and want and os.path.abspath(sdsn) != os.path.abspath(want):
                stats = None
        geo = (stats or {}).get("geometry") or {}
        traces = geo.get("traces", [])
        self.log.appendPlainText(
            f"· done: stats={'yes' if stats else 'MISSING'}, "
            f"final geometry = {len(traces)} traces, "
            f"{len(geo.get('vias', []))} vias, {len(geo.get('unrouted', []))} unrouted"
        )
        # Rebuild the final board. NEVER leave it blank: only clear the live
        # copper once we KNOW there's geometry to replace it with, and if the
        # rebuild throws, fall back to a plain finalize (keep the live view).
        try:
            if traces:
                self.view.clear_copper()
                self.view._final_mode = True
                for net, layer, coords, width in traces:
                    self.view.add_trace(net, layer, [tuple(c) for c in coords], width)
                for net, x, y, dia in geo.get("vias", []):
                    self.view.add_via(net, x, y, dia)
                self.view.finalize()
                self.view.draw_unrouted(geo.get("unrouted", []))
            else:
                # no geometry (cancel/crash) — keep the live board, strip glow
                self.view.finalize()
        except Exception as e:  # a redraw error must never blank the board
            self.log.appendPlainText(
                f"· final redraw error (kept live view): {e}"
            )
            self.view.finalize()
        if stats is None:
            if self.status.text() != "cancelled":
                self.status.setText(f"routing exited (code {code}) — see log")
            return
        self.last_stats = stats
        self.save_btn.setEnabled(True)
        r = stats["routing"]
        self.progress.setValue(100)
        self.progress.setFormat(f"done · {r['routed']}/{r['total']} connections")
        msg = (
            f"done: {r['routed']}/{r['total']} in {r['seconds']:.0f}s — "
            f"session written to {os.path.basename(stats['ses'])}"
        )
        if traces:
            n_un = len(geo.get("unrouted", []))
            if n_un:
                msg += f"  ·  {n_un} unrouted (shown red)"
        self.status.setText(msg)
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
