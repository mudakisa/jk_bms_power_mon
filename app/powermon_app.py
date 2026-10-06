"""PowerMon desktop app (Linux) - a native Qt window over the DB the collector writes.

Same data and layout as the web dashboard (web/), which stays for every other platform.
Both read through readings.py, so the numbers match. Read-only, like the rest.

    .venv/bin/pip install -r requirements-app.txt
    .venv/bin/python app/powermon_app.py
"""
import os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))      # readings.py / config.py in the project root
from readings import query, banks, set_paused, LABELS

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QMainWindow, QMenu, QProgressBar,
                               QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

RANGES = [("30m", 30), ("1h", 60), ("6h", 360), ("24h", 1440), ("7d", 10080)]
REFRESH = [("Off", 0), ("10s", 10_000), ("30s", 30_000), ("1m", 60_000)]

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
            f'<span style="color:{c}">●</span>&nbsp;<span style="color:#9aa0aa">{n}</span>'
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
        self.curves = []
        for _, color in series:
            kw = dict(fillLevel=0, brush=pg.mkBrush(color + "22")) if fill else {}
            pen = pg.mkPen(color, width=2)
            pen.setCapStyle(Qt.FlatCap)        # segment ends don't overlap into bright dots
            # autoDownsampleFactor 1: "peak" keeps ~2 points per pixel (default 5 -> ~10)
            self.curves.append(pi.plot([], [], pen=pen, connect="finite",
                                       autoDownsampleFactor=1.0, **kw))
        self.vline = pg.InfiniteLine(angle=90, pen=pg.mkPen("#555", width=1))
        self.tip = pg.TextItem(fill=pg.mkBrush("#1d2128e6"), border=pg.mkPen(LINE))
        for item in (self.vline, self.tip):
            pi.addItem(item, ignoreBounds=True)
            item.hide()
        self.plot.scene().sigMouseMoved.connect(self.hover)
        self.plot.on_leave = self.unhover

    def set(self, t, ys):
        self.t, self.ys = t, ys
        for curve, y in zip(self.curves, ys):
            curve.setData(t, y)
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


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.tray = None                        # before anything that fires changeEvent
        self.setWindowTitle("PowerMon")
        self.resize(1000, 633)
        self.setMinimumSize(800, 560)
        self.minutes = 360
        self.bank = LABELS[0][0]
        self.paused = False
        self.timer = QTimer(self, timeout=self.load)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- header: title, freshness, range, refresh
        hdr = QFrame()
        hdr.setObjectName("header")
        h = QHBoxLayout(hdr)
        h.setContentsMargins(18, 12, 18, 12)
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
        h.addWidget(self.dot)
        h.addWidget(self.age)
        self.link = QPushButton("—")
        self.link.setObjectName("btn")
        self.link.setCursor(Qt.PointingHandCursor)
        self.link.setToolTip("Release the BLE link so the phone app can connect, or take it back")
        self.link.clicked.connect(self.toggle_link)
        h.addWidget(self.link)
        h.addStretch(1)
        seg = QFrame()
        seg.setObjectName("seg")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.ranges = QButtonGroup(self)
        for name, minutes in RANGES:
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(minutes == self.minutes)
            b.setCursor(Qt.PointingHandCursor)
            self.ranges.addButton(b, minutes)
            sl.addWidget(b)
        self.ranges.idClicked.connect(self.set_range)
        h.addWidget(seg)
        h.addWidget(label("refresh", "muted"))
        self.refresh = QComboBox()
        for name, ms in REFRESH:
            self.refresh.addItem(name, ms)
        self.refresh.setCurrentIndex(2)
        self.refresh.currentIndexChanged.connect(self.schedule)
        h.addWidget(self.refresh)
        outer.addWidget(hdr)

        main = QVBoxLayout()
        main.setContentsMargins(12, 12, 12, 12)
        main.setSpacing(12)
        outer.addLayout(main, 1)

        # ---- stat cards
        stats = QHBoxLayout()
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
        grid = QGridLayout()
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
        main.addLayout(grid, 1)

        self.load()
        self.schedule()

    def setup_tray(self, icon):
        """Live in the tray: start there, minimize back there. On GNOME the tray needs
        the AppIndicator extension (on by default in Ubuntu)."""
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu(self)
        menu.addAction("Show", self.bring_up)
        menu.addAction("Quit", QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_click)
        self.tray.setToolTip("PowerMon")
        self.tray.show()

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

    def set_bank(self, i):
        self.bank = self.bank_ids[i]
        self.load()

    def toggle_link(self):
        set_paused(self.bank, not self.paused)
        self.load()

    def set_range(self, minutes):
        self.minutes = minutes
        self.load()

    def schedule(self):
        ms = self.refresh.currentData()
        self.timer.stop()
        if ms:
            self.timer.start(ms)

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
        self.paused = next(b["paused"] for b in banks() if b["id"] == self.bank)
        self.link.setText("Connect" if self.paused else "Disconnect")
        try:
            d = query(self.minutes, self.bank)
        except Exception as e:
            self.set_dot("#d9534f")
            self.age.setText(f"read error: {e}")
            return
        L = d["latest"]
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

        t = np.array(d["t"], dtype=float) / 1000
        arr = lambda k: np.array(d[k], dtype=float)
        self.ch["v"].set(t, [arr("pack_v")])
        self.ch["a"].set(t, [arr("current_a")])
        self.ch["soc"].set(t, [arr("soc_pct")])
        self.ch["t"].set(t, [arr("mos_temp_c"), arr("temp1_c"), arr("temp2_c")])
        self.ch["cells"].set(t, [arr("cell_min_v"), arr("cell_avg_v"), arr("cell_max_v")])
        self.ch["d"].set(t, [arr("delta_mv")])


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

    if QSystemTrayIcon.isSystemTrayAvailable():
        w.setup_tray(app.windowIcon())          # start in the tray
    else:
        w.show()                                # no tray to come back from
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
