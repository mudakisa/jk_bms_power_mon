"""PowerMon desktop app (Linux) - a native Qt window over the DB the collector writes.

Same data and layout as the web dashboard (web/), which stays for every other platform.
Both read through readings.py, so the numbers match. Read-only, like the rest.

    .venv/bin/pip install -r requirements-app.txt
    .venv/bin/python app/powermon_app.py          # opens the window
    .venv/bin/python app/powermon_app.py --tray   # starts hidden in the tray (login autostart)
"""
import os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))      # readings.py / config.py in the project root
from readings import query, latest, banks, latest_soc, paused, set_paused, LABELS

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QMainWindow, QMenu, QProgressBar,
                               QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

RANGES = [("30m", 30), ("1h", 60), ("3h", 180), ("6h", 360), ("12h", 720), ("24h", 1440),
          ("2d", 2880), ("7d", 10080)]
CARDS_MS, CHARTS_MS = 1000, 30_000     # cards: the newest row, live; charts: the whole range

BG, CARD, LINE, TXT, MUTED = "#0e0f12", "#16181d", "#23262d", "#e6e6e6", "#8a8f98"
GRID = "#262a31"
C = dict(v="#5b9bd5", a="#e0883c", soc="#3bbf6b", mos="#e0883c", t1="#5b9bd5", t2="#b06bd9",
         cmin="#e0883c", cavg="#3bbf6b", cmax="#5b9bd5", d="#3bbf6b")

QSS = f"""
#root {{ background:{BG}; }}
QLabel {{ color:{TXT}; font-size:14px; }}
#header {{ background:#121419; border-bottom:1px solid {LINE}; }}
#card {{ background:{CARD}; border:1px solid {LINE}; border-radius:10px; }}
#k, #muted {{ color:{MUTED}; font-size:12px; }}
#v {{ font-size:30px; font-weight:600; }}
#cv {{ font-size:18px; font-weight:600; }}
#h2 {{ color:{MUTED}; font-size:13px; }}
#seg {{ border:1px solid {LINE}; border-radius:7px; }}
#seg QPushButton {{ background:transparent; color:{MUTED}; border:0; padding:6px 11px; font-size:13px; }}
#seg QPushButton:hover {{ background:#1d2128; color:{TXT}; }}
#seg QPushButton:checked {{ background:#2a4c7a; color:#fff; }}
#btn {{ background:#1d2128; color:{TXT}; border:1px solid {LINE}; border-radius:7px; padding:6px 11px; font-size:13px; }}
#btn:hover {{ background:#252a33; }}
#btn[accent="true"] {{ background:#2a4c7a; border-color:#2a4c7a; color:#fff; }}
#veil {{ background:rgba(14, 15, 18, 170); color:{TXT}; font-size:22px; font-weight:600; }}
QComboBox {{ background:#1d2128; color:{TXT}; border:1px solid {LINE}; border-radius:7px; padding:4px 8px; }}
QComboBox::drop-down {{ border:0; width:18px; }}
QComboBox::down-arrow {{ image:url({os.path.join(HERE, "arrow.svg")}); width:9px; height:6px; }}
QComboBox QAbstractItemView {{ background:#1d2128; color:{TXT}; selection-background-color:#2a4c7a; }}
QToolTip {{ background:#1d2128; color:{TXT}; border:1px solid {LINE}; }}
QProgressBar {{ background:{LINE}; border:0; border-radius:3px; }}
"""


def color_soc(s): return "#d9534f" if s < 20 else "#e0883c" if s < 40 else "#e6c84f" if s < 60 else "#3bbf6b"
def color_v(v): return "#d9534f" if v < 12 else "#e0883c" if v < 12.8 else "#3bbf6b"
def unit(u): return f'<span style="font-size:15px; color:{MUTED}; font-weight:400"> {u}</span>'
def signed(x, nd): return f"{'+' if x >= 0 else ''}{x:.{nd}f}"


def signal_html(link, rssi):
    """BLE signal as four bars (filled up to the level, coloured by it) and dBm."""
    if not link:
        return f'<span style="color:{MUTED}">no link</span>'
    if rssi is None:                    # linked, but the signal can't be read here
        return ""
    n = sum(rssi >= t for t in (-90, -80, -70, -60))
    col = "#3bbf6b" if n >= 3 else "#e6c84f" if n == 2 else "#d9534f"
    bars = "".join(f'<span style="color:{col if i < n else LINE}">{c}</span>'
                   for i, c in enumerate("▂▄▆█"))
    return f'{bars}<span style="color:{MUTED}"> {rssi} dBm</span>'


def fmt_dur(h):
    m = round(h * 60)
    if m < 60:
        return f"{m}{unit('m')}"
    if m < 2880:
        return f"{m // 60}{unit('h')} {m % 60}{unit('m')}"
    return f"{m // 1440}{unit('d')} {m % 1440 // 60}{unit('h')}"


def fmt_at(ms):
    at, now = ms / 1000, time.time()
    if time.strftime("%x", time.localtime(at)) == time.strftime("%x"):
        return time.strftime("%H:%M", time.localtime(at))
    return time.strftime("%a %H:%M" if at - now < 6 * 86400 else "%d.%m %H:%M", time.localtime(at))


# Tray menu levels. GNOME draws that menu itself as plain text plus one square icon per
# item (no widgets, no markup), so the bar gets its colour from emoji squares.
TRAY_LEVELS = [(25, "#d9534f", "🟥"), (60, "#e6c84f", "🟨"), (101, "#3bbf6b", "🟩")]


def tray_level(soc):
    return next((color, square) for limit, color, square in TRAY_LEVELS if soc < limit)


def bridge(t, y, breaks):
    """Pairs of points over the holes (last reading before -> first after), for a dashed
    line with connect="pairs"; the solid line itself breaks there."""
    bx, by = [], []
    for i in breaks:
        if 0 < i < len(t) - 1 and np.isfinite(y[i - 1]) and np.isfinite(y[i + 1]):
            bx += [t[i - 1], t[i + 1]]
            by += [y[i - 1], y[i + 1]]
    return np.array(bx, dtype=float), np.array(by, dtype=float)


def dashed(color, width):
    return pg.mkPen(color + "aa", width=width, style=Qt.DashLine)


def battery_icon(soc, color):
    """A small battery filled to `soc` in `color`, for the tray menu."""
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(TXT), 2))
    p.drawRoundedRect(2, 9, 24, 14, 3, 3)
    p.fillRect(27, 13, 3, 6, QColor(TXT))
    p.fillRect(5, 12, round(18 * soc / 100), 8, QColor(color))
    p.end()
    return QIcon(pm)


def card(margins=(14, 12, 14, 12), spacing=4):
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return f, lay


def label(text="", name=None):
    w = QLabel(text)
    w.setTextFormat(Qt.RichText)
    if name:
        w.setObjectName(name)
    return w


class Stat:
    """A stat card: caption, big value, optional extra widget under it."""
    def __init__(self, title):
        self.frame, self.lay = card()
        self.lay.addWidget(label(title.upper(), "k"))
        self.v = label("—", "v")
        self.lay.addWidget(self.v)

    def set(self, html, color=TXT):
        self.v.setText(f'<span style="color:{color}">{html}</span>')


class TimeAxis(pg.AxisItem):
    """Time axis on round local-time steps, ~60 px per label. pyqtgraph's DateAxisItem
    picks odd minute steps on a narrow chart: 6 h over ~250 px showed only the date."""
    STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800]

    def tickValues(self, minVal, maxVal, size):
        n = max(2, int(size // 60))
        step = next((s for s in self.STEPS if (maxVal - minVal) / s <= n), self.STEPS[-1])
        off = time.localtime(minVal).tm_gmtoff          # align steps to local midnight
        first = ((minVal + off) // step + 1) * step - off
        return [(step, list(np.arange(first, maxVal, step)))]

    def tickStrings(self, values, scale, spacing):
        def fmt(v):
            t = time.localtime(v)
            day = spacing >= 86400 or (t.tm_hour == 0 and t.tm_min == 0)
            return time.strftime("%a %d" if day else "%H:%M", t)
        return [fmt(v) for v in values]


class Plot(pg.PlotWidget):
    on_leave = None

    def leaveEvent(self, e):
        super().leaveEvent(e)
        if self.on_leave:
            self.on_leave()


class Chart:
    """A chart card: title (+ legend for several series), time axis, hover readout.
    fill: shade down to zero - only for smooth series; under a noisy one it costs ~3x
    per repaint, which makes resizing the window stutter. ymin: pin the axis bottom."""
    def __init__(self, title, series, fmt="{:.2f}", fill=False, yrange=None, ymin=None):
        self.series, self.fmt, self.ymin, self.t, self.ys = series, fmt, ymin, np.array([]), []
        self.frame, lay = card((12, 10, 12, 10), 6)
        # legend items break onto a second line when the card is narrow
        legend = "" if len(series) == 1 else "&nbsp;&nbsp; " + " &nbsp;".join(
            f'<span style="color:{c}">●</span>&nbsp;<span style="color:#9aa0aa">{n.replace(" ", "&nbsp;")}</span>'
            for n, c in series)
        head = label(title + legend, "h2")
        head.setWordWrap(True)
        lay.addWidget(head)
        self.plot = Plot(background=CARD, axisItems={"bottom": TimeAxis("bottom")})
        self.plot.setMinimumHeight(120)
        lay.addWidget(self.plot, 1)
        pi = self.pi = self.plot.getPlotItem()
        pi.setMouseEnabled(False, False)
        pi.setMenuEnabled(False)
        pi.hideButtons()
        pi.setClipToView(True)
        pi.setDownsampling(auto=True, mode="peak")
        tick = QFont()
        tick.setPointSize(8)
        for ax in ("left", "bottom"):
            a = pi.getAxis(ax)
            a.setPen(GRID)
            a.setTextPen("#777")
            a.setStyle(tickFont=tick)
            a.setZValue(-1)                      # grid under the curves, not over the fill
        pi.showGrid(x=True, y=True, alpha=1.0)
        if yrange:
            pi.setYRange(*yrange, padding=0.02)
        self.curves, self.bridges = [], []
        for _, color in series:
            kw = dict(fillLevel=0, brush=pg.mkBrush(color + "22")) if fill else {}
            pen = pg.mkPen(color, width=2)
            pen.setCapStyle(Qt.FlatCap)        # segment ends don't overlap into bright dots
            # autoDownsampleFactor 1: "peak" keeps ~2 points per pixel (default 5 -> ~10)
            self.curves.append(pi.plot([], [], pen=pen, connect="finite",
                                       autoDownsampleFactor=1.0, **kw))
            self.bridges.append(pi.plot([], [], pen=dashed(color, 1.5), connect="pairs"))
        self.vline = pg.InfiniteLine(angle=90, pen=pg.mkPen("#555", width=1))
        self.tip = pg.TextItem(fill=pg.mkBrush("#1d2128e6"), border=pg.mkPen(LINE))
        for item in (self.vline, self.tip):
            pi.addItem(item, ignoreBounds=True)
            item.hide()
        self.plot.scene().sigMouseMoved.connect(self.hover)
        self.plot.on_leave = self.unhover

    def set(self, t, ys, breaks=()):
        self.t, self.ys = t, ys
        for curve, dash, y in zip(self.curves, self.bridges, ys):
            curve.setData(t, y)
            dash.setData(*bridge(t, y, breaks))
        top = max((np.nanmax(y) for y in ys if np.isfinite(y).any()), default=None)
        if self.ymin is not None and top is not None:
            self.pi.setYRange(self.ymin, max(top, self.ymin + 1), padding=0.05)
        if len(t):
            self.pi.setXRange(t[0], t[-1], padding=0)
        self.unhover()

    def hover(self, pos):
        vb = self.pi.getViewBox()
        if not len(self.t) or not vb.sceneBoundingRect().contains(pos):
            return self.unhover()
        x = vb.mapSceneToView(pos).x()
        i = int(np.clip(np.searchsorted(self.t, x), 1, len(self.t) - 1))
        i -= int(x - self.t[i - 1] < self.t[i] - x)
        rows = "".join(
            f'<br><span style="color:{c}">●</span> {n}: ' + ("—" if np.isnan(y[i]) else self.fmt.format(y[i]))
            for (n, c), y in zip(self.series, self.ys))
        stamp = time.strftime("%d.%m %H:%M:%S" if self.t[-1] - self.t[0] > 86400 else "%H:%M:%S",
                              time.localtime(self.t[i]))
        self.tip.setHtml(f'<div style="color:{TXT}; font-size:11px">{stamp}{rows}</div>')
        (x0, x1), (_, y1) = vb.viewRange()
        self.tip.setAnchor((1, 0) if self.t[i] > (x0 + x1) / 2 else (0, 0))
        self.tip.setPos(self.t[i], y1)
        self.vline.setPos(self.t[i])
        self.vline.show()
        self.tip.show()

    def unhover(self):
        self.vline.hide()
        self.tip.hide()


class Spark:
    """A chart thumbnail for a low window: a short title and the line, no axes - only the
    top and bottom values of the line, written at its left edge."""
    def __init__(self, title, colors, fmt):
        self.fmt = fmt
        self.frame, lay = card((12, 6, 12, 6), 2)
        lay.addWidget(label(title, "h2"))
        row = QHBoxLayout()
        row.setSpacing(6)
        edge = QVBoxLayout()
        edge.setSpacing(0)
        self.hi, self.lo = label("", "muted"), label("", "muted")
        self.note = label("", "muted")              # an extra figure above the bottom value
        for w, align in ((self.hi, Qt.AlignTop), (self.note, Qt.AlignBottom),
                         (self.lo, Qt.AlignBottom)):
            w.setAlignment(Qt.AlignRight | align)
        edge.addWidget(self.hi)
        edge.addStretch(1)
        edge.addWidget(self.note)
        edge.addWidget(self.lo)
        row.addLayout(edge)
        self.plot = pg.PlotWidget(background=CARD)
        self.plot.setMinimumHeight(30)
        pi = self.pi = self.plot.getPlotItem()
        pi.hideAxis("left")
        pi.hideAxis("bottom")
        pi.setMouseEnabled(False, False)
        pi.setMenuEnabled(False)
        pi.hideButtons()
        pi.setClipToView(True)
        pi.setDownsampling(auto=True, mode="peak")
        self.curves = [pi.plot([], [], pen=pg.mkPen(c, width=1.5), connect="finite",
                               autoDownsampleFactor=1.0) for c in colors]
        self.bridges = [pi.plot([], [], pen=dashed(c, 1), connect="pairs") for c in colors]
        row.addWidget(self.plot, 1)
        lay.addLayout(row, 1)

    def set(self, t, ys, breaks=()):
        for curve, dash, y in zip(self.curves, self.bridges, ys):
            curve.setData(t, y)
            dash.setData(*bridge(t, y, breaks))
        if len(t):
            self.pi.setXRange(t[0], t[-1], padding=0)
        vals = np.concatenate(ys) if ys else np.array([])
        vals = vals[np.isfinite(vals)]
        if len(vals):                   # the line spans the card, so the labels sit at its ends
            lo, hi = vals.min(), vals.max()
            self.pi.setYRange(lo, hi if hi > lo else lo + 1, padding=0.04)
            self.hi.setText(self.fmt.format(hi))
            self.lo.setText(self.fmt.format(lo))
        else:
            self.hi.setText("")
            self.lo.setText("")


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.tray = None                        # before anything that fires changeEvent
        self.setWindowTitle("PowerMon")
        self.resize(1154, 774)                  # 1442x968 px at a 1.25 desktop scale
        self.minutes = 360
        self.bank = LABELS[0][0]
        self.paused = False
        self.cards_timer = QTimer(self, timeout=self.load_cards, interval=CARDS_MS)
        self.charts_timer = QTimer(self, timeout=self.load_charts, interval=CHARTS_MS)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- header: title, banks, freshness, signal, link | range.
        # The right group shares row 1 while it fits and drops to row 2 when the window
        # is narrow (see fit_header) - squeezed into one row the buttons got clipped.
        hdr = self.hdr = QFrame()
        hdr.setObjectName("header")
        hv = QVBoxLayout(hdr)
        hv.setContentsMargins(18, 12, 18, 12)
        hv.setSpacing(8)
        self.row1, self.row2 = QHBoxLayout(), QHBoxLayout()
        self.row2.addStretch(1)
        hv.addLayout(self.row1)
        hv.addLayout(self.row2)
        self.hleft, self.hright = QWidget(), QWidget()
        h = QHBoxLayout(self.hleft)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(16)
        h.addWidget(label('<span style="font-size:16px; font-weight:600">PowerMon</span>'))
        # bank buttons (labels from config.py) and the connect / disconnect of the selected one
        bseg = QFrame()
        bseg.setObjectName("seg")
        bl = QHBoxLayout(bseg)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        self.bank_group = QButtonGroup(self)
        self.bank_ids = [b for b, _ in LABELS]
        for i, (b, text) in enumerate(LABELS):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setChecked(b == self.bank)
            btn.setCursor(Qt.PointingHandCursor)
            self.bank_group.addButton(btn, i)
            bl.addWidget(btn)
        self.bank_group.idClicked.connect(self.set_bank)
        h.addWidget(bseg)
        self.dot = QLabel()
        self.dot.setFixedSize(9, 9)
        self.age = label("—", "muted")
        self.sig = label("", "muted")
        self.sig.setToolTip("Bluetooth signal of the link to this battery")
        h.addWidget(self.dot)
        h.addWidget(self.age)
        h.addWidget(self.sig)
        self.link = QPushButton("—")
        self.link.setObjectName("btn")
        self.link.setCursor(Qt.PointingHandCursor)
        self.link.setToolTip("Release the BLE link so the phone app can connect, or take it back")
        self.link.clicked.connect(self.toggle_link)
        h.addWidget(self.link)
        # fixed widths for the texts that change, so the header doesn't jump
        for w, longest in ((self.age, "paused, link free"), (self.sig, "▂▄▆█ -100 dBm")):
            w.ensurePolished()
            w.setMinimumWidth(w.fontMetrics().horizontalAdvance(longest) + 6)
        h = QHBoxLayout(self.hright)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(16)
        h.addWidget(label("range", "muted"))
        self.range_box = QComboBox()
        for name, minutes in RANGES:
            self.range_box.addItem(name, minutes)
        self.range_box.setCurrentIndex(self.range_box.findData(self.minutes))
        self.range_box.currentIndexChanged.connect(lambda _: self.set_range(self.range_box.currentData()))
        h.addWidget(self.range_box)
        self.row1.addWidget(self.hleft)
        self.row1.addStretch(1)
        self.row1.addWidget(self.hright)
        self.two_rows = False
        outer.addWidget(hdr)

        self.body = QWidget()
        main = QVBoxLayout(self.body)
        main.setContentsMargins(12, 12, 12, 12)
        main.setSpacing(12)
        outer.addWidget(self.body, 1)
        # a disconnected bank: everything below the header under a dimming veil
        self.veil = label("Disconnected<br><span style=\"font-size:13px; font-weight:400; "
                          f"color:{MUTED}\">BLE link is free for the phone app</span>", "veil")
        self.veil.setParent(self.body)
        self.veil.setAlignment(Qt.AlignCenter)
        self.veil.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.veil.hide()
        self.body.installEventFilter(self)

        # ---- stat cards
        stats = self.stats_row = QHBoxLayout()
        stats.setSpacing(12)
        self.s_soc, self.s_v, self.s_a, self.s_w, self.s_eta = (
            Stat(n) for n in ("State of charge", "Pack voltage", "Current", "Power", "ETA"))
        self.soc_bar = QProgressBar()
        self.soc_bar.setTextVisible(False)
        self.soc_bar.setFixedHeight(6)
        self.s_soc.lay.addSpacing(6)
        self.s_soc.lay.addWidget(self.soc_bar)
        self.eta_sub = label("", "muted")
        self.s_eta.lay.addWidget(self.eta_sub)
        for s in (self.s_soc, self.s_v, self.s_a, self.s_w, self.s_eta):
            s.lay.addStretch(1)
            stats.addWidget(s.frame, 1)
        main.addLayout(stats)

        # ---- per-cell voltages
        self.cells_row = QHBoxLayout()
        self.cells_row.setSpacing(12)
        self.cells = []
        main.addLayout(self.cells_row)

        # ---- charts
        self.charts = QWidget()
        grid = QGridLayout(self.charts)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        self.ch = {
            "v": Chart("Pack voltage", [("Pack V", C["v"])], "{:.3f} V"),
            "a": Chart("Current (load behaviour)", [("Current", C["a"])], "{:+.2f} A"),
            "soc": Chart("State of charge", [("SoC", C["soc"])], "{:.0f} %", fill=True, yrange=(0, 100)),
            "t": Chart("Temperatures", [("MOSFET", C["mos"]), ("Sensor 1", C["t1"]),
                                        ("Sensor 2", C["t2"])], "{:.1f} °C"),
            "cells": Chart("Cell voltages (min / avg / max)", [("Cell min", C["cmin"]),
                           ("Cell avg", C["cavg"]), ("Cell max", C["cmax"])], "{:.3f} V"),
            "d": Chart("Cell balance delta (mV)", [("Delta", C["d"])], "{:.0f} mV", ymin=0),
        }
        for i, c in enumerate(self.ch.values()):
            grid.addWidget(c.frame, i // 3, i % 3)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        main.addWidget(self.charts, 1)
        # thumbnails for a window too low for the charts but not just for the cards
        self.minis = QWidget()
        ml = QHBoxLayout(self.minis)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(12)
        self.mini = {"soc": Spark("SoC", [C["soc"]], "{:.0f} %"),
                     "a": Spark("Current", [C["a"]], "{:+.1f} A"),
                     "v": Spark("Voltage", [C["v"]], "{:.2f} V"),
                     "t": Spark("Temperatures", [C["mos"], C["t1"], C["t2"]], "{:.1f} °C")}
        for m in self.mini.values():
            ml.addWidget(m.frame, 1)
        self.minis.hide()
        self.mode = "full"
        main.addWidget(self.minis, 1)
        main.addStretch(0)          # gets stretch 1 when the charts hide: cards stay at the top
        self.tail = main.count() - 1

        self.load()
        self.cards_timer.start()
        self.charts_timer.start()

    def setup_tray(self, icon):
        """Live in the tray: start there, minimize back there. On GNOME the tray needs
        the AppIndicator extension (on by default in Ubuntu)."""
        self.tray = QSystemTrayIcon(icon, self)
        # one row per bank: name, a text bar of its charge, a battery icon; click = its page
        menu = QMenu(self)
        self.bank_actions = {}
        for b, text in LABELS:
            a = menu.addAction(text)
            a.setIconVisibleInMenu(True)        # the GTK theme hides menu icons otherwise
            a.triggered.connect(lambda _=False, b=b: self.open_bank(b))
            self.bank_actions[b] = a
        menu.addSeparator()
        menu.addAction("Quit", QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_click)
        self.tray.setToolTip("PowerMon")
        self.update_tray_menu()                 # the first load() ran before the tray existed
        self.tray.show()

    def update_tray_menu(self):
        now, off = latest_soc(), paused()
        for b, a in self.bank_actions.items():
            text = dict(LABELS)[b]
            if b not in now:
                a.setText(f"{text}   no data")
                continue
            soc, ts = now[b]
            n = round(soc * 7 / 100)            # 7 segments
            note = "   paused" if b in off else "   stale" if time.time() - ts > 120 else ""
            color, square = tray_level(soc)
            a.setText(f"{text}   {square * n}{'⬛' * (7 - n)}  {soc} %{note}")
            a.setIcon(battery_icon(soc, color))

    def open_bank(self, bank):
        i = self.bank_ids.index(bank)
        self.bank_group.button(i).setChecked(True)
        self.set_bank(i)
        self.bring_up()

    def tray_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.bring_up()

    def bring_up(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, e):
        super().changeEvent(e)
        if self.tray and e.type() == QEvent.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)         # minimize = into the tray

    def fit_header(self):
        """The range group shares the header row while it fits, else takes a second row.
        Measured here, not in __init__: sizes are right only once the style applies."""
        self.setMinimumWidth(max(self.body.minimumSizeHint().width(),   # narrowest: the body,
                                 self.hleft.sizeHint().width() + 36,    # or one header group
                                 self.hright.sizeHint().width() + 36))  # per row
        two = self.width() < self.hleft.sizeHint().width() + self.hright.sizeHint().width() + 16 + 36
        if two != self.two_rows:
            self.two_rows = two
            (self.row1 if two else self.row2).removeWidget(self.hright)
            (self.row2 if two else self.row1).addWidget(self.hright)

    def fit_body(self):
        """Below the two rows of cards (stats, cells): the charts while they fit, else a row
        of thumbnails, else nothing - the window then shrinks to the cards."""
        cards = self.stats_row.minimumSize().height() + self.cells_row.minimumSize().height()
        self.setMinimumHeight(self.hdr.sizeHint().height() + cards + 36)   # body margins + gap
        room = self.body.height() - cards - 48
        mode = ("full" if room >= self.charts.minimumSizeHint().height() else
                "mini" if room >= self.minis.minimumSizeHint().height() else "cards")
        if mode != self.mode:
            self.mode = mode
            self.charts.setVisible(mode == "full")
            self.minis.setVisible(mode == "mini")
            self.body.layout().setStretch(self.tail, 1 if mode == "cards" else 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit_header()

    def eventFilter(self, obj, e):
        if obj is self.body and e.type() == QEvent.Resize:     # the body's own, current size
            self.veil.setGeometry(self.body.rect())
            self.fit_body()
        return False

    def show_paused(self, on):
        self.veil.setVisible(on)
        self.veil.raise_()
        self.link.setProperty("accent", on)     # Connect stands out when paused
        self.link.style().unpolish(self.link)
        self.link.style().polish(self.link)

    def set_bank(self, i):
        self.bank = self.bank_ids[i]
        self.load()

    def toggle_link(self):
        set_paused(self.bank, not self.paused)
        self.load()

    def set_range(self, minutes):
        self.minutes = minutes
        self.load_charts()

    def set_dot(self, color):
        self.dot.setStyleSheet(f"background:{color}; border-radius:4px;")

    def show_cells(self, cells):
        if len(self.cells) != len(cells):
            while self.cells_row.count():
                self.cells_row.takeAt(0).widget().setParent(None)   # off screen right away
            self.cells = []
            for i in range(len(cells)):
                f = QFrame()
                f.setObjectName("card")
                lay = QHBoxLayout(f)
                lay.setContentsMargins(12, 8, 12, 8)
                lay.addWidget(label(f"C{i + 1}", "muted"))
                lay.addStretch(1)
                v = label("", "cv")
                lay.addWidget(v)
                self.cells_row.addWidget(f, 1)
                self.cells.append(v)
        if not cells:
            return
        mn, mx, spread = min(cells), max(cells), len(cells) > 1
        for v, x in zip(self.cells, cells):
            col = C["cmin"] if spread and x == mn else C["cmax"] if spread and x == mx else TXT
            v.setText(f'<span style="color:{col}">{x:.3f}</span>'
                      f'<span style="font-size:11px; color:{MUTED}; font-weight:400"> V</span>')

    def show_eta(self, e):
        border = {"charge": "#3bbf6b", "discharge": "#e0883c"}.get(e["state"]) if e else None
        self.s_eta.frame.setStyleSheet(f"#card {{ border:1px solid {border}; }}" if border else "")
        if not e:
            self.s_eta.set("—")
            self.eta_sub.setText("")
            self.s_eta.frame.setToolTip("")
            return
        a = f"{signed(e['amps'], 1)} A"
        self.s_eta.frame.setToolTip(
            f"At the mean current of the last {round(e['window_s'] / 60)} min: to 0 % SoC "
            f"when discharging, to 100 % when charging (BMS coulomb counter).")
        if e["state"] == "idle":
            self.s_eta.set("—")
            self.eta_sub.setText(f"idle, {a}")
            return
        self.s_eta.set(fmt_dur(e["hours"]))
        self.eta_sub.setText(f"{'full' if e['state'] == 'charge' else 'empty'} at {fmt_at(e['at'])}, {a}")

    def load(self):
        self.load_cards()
        self.load_charts()

    def load_cards(self):
        """Status, cards and tray from the bank's newest row - every second."""
        me = next(b for b in banks() if b["id"] == self.bank)
        self.paused = me["paused"]
        self.sig.setText("" if self.paused else signal_html(me["link"], me["rssi"]))
        self.link.setText("Connect" if self.paused else "Disconnect")
        self.show_paused(self.paused)
        try:
            L = latest(self.bank)
        except Exception as e:
            self.set_dot("#d9534f")
            self.age.setText(f"read error: {e}")
            return
        if L:
            self.set_dot("#6b7079" if self.paused else "#d9534f" if L["age_s"] > 120 else "#3bbf6b")
            self.age.setText("paused, link free" if self.paused
                             else f"updated {round(L['age_s'])}s ago" if L["age_s"] < 90
                             else f"stale {round(L['age_s'])}s")
            soc = L["soc_pct"]
            self.s_soc.set(f"{soc}{unit('%')}", color_soc(soc))
            self.soc_bar.setValue(soc)
            self.soc_bar.setStyleSheet(f"QProgressBar::chunk {{ background:{color_soc(soc)}; border-radius:3px; }}")
            self.s_v.set(f"{L['pack_v']:.2f}{unit('V')}", color_v(L["pack_v"]))
            self.s_a.set(f"{signed(L['current_a'], 1)}{unit('A')}")
            self.s_w.set(f"{'+' if L['power_w'] >= 0 else ''}{round(L['power_w'])}{unit('W')}")
            self.show_eta(L.get("eta"))
            if self.tray:
                self.update_tray_menu()
                eta = self.eta_sub.text()
                self.tray.setToolTip(f"{dict(LABELS)[self.bank]}: {soc} %" + (f", {eta}" if eta else ""))
            if L["cells"]:
                self.show_cells(L["cells"])
        else:                           # no readings for this bank: don't show the last bank's
            self.set_dot("#6b7079" if self.paused else "#d9534f")
            self.age.setText("paused, link free" if self.paused else "no data")
            for s in (self.s_soc, self.s_v, self.s_a, self.s_w):
                s.set("—")
            self.soc_bar.setValue(0)
            self.show_eta(None)
            self.show_cells([])

    def load_charts(self):
        """Charts and thumbnails: every reading of the range - every 30 s, or on a change."""
        try:
            d = query(self.minutes, self.bank)
        except Exception:
            return                              # load_cards reports read errors
        t = np.array(d["t"], dtype=float) / 1000
        arr = lambda k: np.array(d[k], dtype=float)
        br = d["breaks"]                            # holes in the data: bridged dashed
        self.ch["v"].set(t, [arr("pack_v")], br)
        self.ch["a"].set(t, [arr("current_a")], br)
        self.ch["soc"].set(t, [arr("soc_pct")], br)
        self.ch["t"].set(t, [arr("mos_temp_c"), arr("temp1_c"), arr("temp2_c")], br)
        self.ch["cells"].set(t, [arr("cell_min_v"), arr("cell_avg_v"), arr("cell_max_v")], br)
        self.ch["d"].set(t, [arr("delta_mv")], br)
        self.mini["soc"].set(t, [arr("soc_pct")], br)
        self.mini["a"].set(t, [arr("current_a")], br)
        self.mini["v"].set(t, [arr("pack_v")], br)
        cmin = arr("cell_min_v")                    # the lowest cell decides the BMS cutoff
        self.mini["v"].note.setText(f'<span style="color:{C["cmin"]}">{np.nanmin(cmin):.3f} V</span>'
                                    if np.isfinite(cmin).any() else "")
        self.mini["t"].set(t, [arr("mos_temp_c"), arr("temp1_c"), arr("temp2_c")], br)


INSTANCE = f"powermon-app-{os.getuid()}"


def main():
    # argv[0] "powermon" sets the X11 WM_CLASS, matching StartupWMClass in the .desktop file
    app = QApplication(["powermon"] + sys.argv[1:])
    probe = QLocalSocket()
    probe.connectToServer(INSTANCE)
    if probe.waitForConnected(300):     # already running (maybe in the tray): it shows itself
        return
    app.setApplicationName("PowerMon")
    app.setDesktopFileName("powermon")
    app.setWindowIcon(QIcon(os.path.join(HERE, "powermon.svg")))
    app.setStyleSheet(QSS)
    # 2 px antialiased lines drawn as segments, not as a stroked path: ~10x cheaper repaint
    pg.setConfigOptions(antialias=True, segmentedLineMode="on")
    w = Main()
    server = QLocalServer(w)
    QLocalServer.removeServer(INSTANCE)        # a stale socket left by a crashed run
    server.listen(INSTANCE)

    def second_launch():
        server.nextPendingConnection()
        w.bring_up()
    server.newConnection.connect(second_launch)

    tray = QSystemTrayIcon.isSystemTrayAvailable()
    if tray:
        w.setup_tray(app.windowIcon())
    if not (tray and "--tray" in sys.argv):     # a click on the launcher opens the window;
        w.show()                                # --tray (login autostart) stays hidden
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
