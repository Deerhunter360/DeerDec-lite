"""CGEN screens. Add a new look = subclass PaintScreen (or _BaseScreen) and register it in STYLES.

  easyplus - black & white EASyPLUS header / ticker / originator reveal (eas.ttf)
  dasdec   - DASDEC-II paged slides (Luxi Mono)
  easycap  - blue screen, paged body text, "- n of N -"
  vds      - green screen, header / event / originator, paged details
  cems1000 - blue 4:3 screen, "EAS Transmission" line-by-line text
  xfinity  - black screen, "EMERGENCY ALERT" + colored crawl band
  psc902   - blue screen, big event text + crawl band
  atari800 - black screen, pixel text, BLINKING event colored by level
  crawl    - generic crawl on a colored band
"""
import json
import math
import os
import threading
from pathlib import Path

from PySide6.QtCore import QSize, QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QImage, QPixmap, QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication,
                           QLinearGradient, QPainter, QPen, QPolygonF)
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

ORIGINATOR_NAMES = {
    "PEP": "Primary Entry Point System",
    "CIV": "Civil Authorities",
    "WXR": "National Weather Service",
    "EAS": "EAS Participant",
}

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
_style = {"family": "EAS OSD Mono", "classic_scale": 1.0, "custom": None,
          "digital_font": None, "easycap": None, "atari": None, "seconds": 60, "screen": -1, "windowed": False}


# ------------------------------------------------------------------ setup helpers
def _register_font(filename):
    path = FONT_DIR / filename
    if not path.exists():
        return None
    fid = QFontDatabase.addApplicationFont(str(path))
    fams = QFontDatabase.applicationFontFamilies(fid) if fid != -1 else []
    return fams[0] if fams else None


def configure(cfg):
    global _AVAIL
    _FONT_CACHE.clear()
    _STRIPS.clear()
    _AVAIL = None                      # fonts get registered below; re-read the installed list
    _style.update(family=cfg.cgen_font, classic_scale=cfg.classic_scale,
                  seconds=cfg.cgen_seconds, screen=cfg.cgen_screen,
                  windowed=getattr(cfg, "laptop_only", False),
                  bulletin=getattr(cfg, "bulletin_station", "KDEER 12"),
                  corner=getattr(cfg, "callsign", "") or "DEERDEC",
                  atari_scale=getattr(cfg, "atari_scale", 1.0))
    # Classic uses fonts/eas.ttf only; Digital uses the Luxi Mono files only.
    _style["custom"] = _register_font("eas.ttf")
    _style["easycap"] = _register_font("easycap.ttf")      # optional pixel fonts
    _style["atari"] = _register_font("atari.ttf")
    _style["digital_font"] = None
    for name in ("luximr.ttf", "luximb.ttf", "luximri.ttf", "luximbi.ttf"):
        fam = _register_font(name)
        _style["digital_font"] = _style["digital_font"] or fam


def place_on_screen(widget):
    """Full-screen on the chosen monitor (e.g. the projector), or - LAPTOP ONLY - an ordinary resizable window.
    Safe to call again whenever the monitor or the mode changes: the window is taken out of full screen first,
    so it really moves to the other monitor and can be resized."""
    windowed = bool(_style.get("windowed"))
    if windowed:
        if not getattr(widget, "_win_mode", False):
            was = widget.isVisible()
            widget._orig_flags = widget.windowFlags()
            widget.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint |
                                  Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
            widget.setWindowTitle("DeerDec - LAPTOP ONLY")
            widget._win_mode = True
            widget.setMinimumSize(320, 180)
            widget.resize(960, 540)
            if was:
                widget.hide()
        if widget.isFullScreen() or widget.isMaximized():
            widget.showNormal()
        if not widget.isVisible():
            widget.showNormal()
        return
    if getattr(widget, "_win_mode", False):
        widget.hide()
        widget.setWindowFlags(widget._orig_flags)
        widget.setMinimumSize(0, 0)
        widget._win_mode = False
    screens = QGuiApplication.screens()
    i = _style["screen"]
    if 0 <= i < len(screens):
        target = screens[i]
        if widget.isFullScreen() and widget.screen() is not target:
            widget.showNormal()                       # leave full screen first, or it stays on the old monitor
        widget.setScreen(target)
        widget.move(target.geometry().topLeft())
        widget.resize(target.geometry().size())
    if not widget.isVisible() or not widget.isFullScreen():     # a hidden window still remembers 'full screen'
        widget.showFullScreen()


def _switch_keys(widget, e):
    """C = EASyPLUS, D = DASDEC, W = crawl, 1-9 = styles in menu order."""
    if not widget.switch_cb:
        return False
    k, letters = e.key(), {Qt.Key_C: "easyplus", Qt.Key_D: "dasdec", Qt.Key_W: "crawl"}
    if k in letters:
        widget.switch_cb(letters[k])
        return True
    if Qt.Key_1 <= k <= Qt.Key_9:
        keys = list(STYLES)
        if k - Qt.Key_1 < len(keys):
            widget.switch_cb(keys[k - Qt.Key_1])
            return True
    return False


def font_px(px, bold=False, family=None):
    """Pixel-sized font so text scales with the screen, not with point sizes."""
    fam = family or _style["custom"] or _style["family"]
    f = QFont(fam)
    f.setPixelSize(max(8, int(px)))
    f.setBold(bold)
    if not _style["custom"] and not family:
        f.setStyleStrategy(QFont.NoAntialias | QFont.PreferBitmap)
    return f


def article_for(word):
    return "an" if word and word[0].lower() in "aeiou" else "a"


# ------------------------------------------------------------------ shared widgets
class MarqueeLabel(QWidget):
    """Seamless right-to-left ticker. Time-based (smooth even if frames are late) and drawn from
    a pre-rendered pixmap. `speed` is pixels per 16 ms frame, as before."""

    def __init__(self, parent=None, bg="black", fg="white"):
        super().__init__(parent)
        self._text, self._offset = "", 0.0
        self._speed, self._gap = 3, 120
        self._font = font_px(48, True)
        self.bg, self.fg = bg, fg
        self._clock = QElapsedTimer()
        self._last = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._advance)
        self._timer.start(16)

    def configure(self, font, speed, gap):
        self._font, self._speed, self._gap = font, max(1, int(speed)), int(gap)
        self.update()

    def setText(self, text):
        self._text = text or ""
        self._offset = float(self.width())
        self._clock.start()
        self._last = 0
        self.update()

    def _advance(self):
        if not self._text:
            return
        now = self._clock.elapsed() if self._clock.isValid() else 0
        dt, self._last = now - self._last, now
        st = crawl_strip(self, self._text, self._font, self.fg, self.height())
        period = st.w + self._gap
        self._offset -= self._speed * 62.5 * dt / 1000.0
        if self._offset <= -period:
            self._offset += period
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self.bg))
        if not self._text:
            return
        st = crawl_strip(p, self._text, self._font, self.fg, self.height())
        period = st.w + self._gap
        x = self._offset
        while x < self.width():
            st.draw(p, x, 0, 0, self.width())
            x += period


class _BaseScreen(QWidget):
    switch_cb = None

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.close_timer = QTimer(self)
        self.close_timer.setSingleShot(True)
        self.close_timer.timeout.connect(self.hide)

    def keyPressEvent(self, e):
        if _switch_keys(self, e):
            return
        if e.key() == Qt.Key_Escape:
            self.hide()

    def mousePressEvent(self, _):
        self.hide()

    def hideEvent(self, e):
        self.close_timer.stop()
        e.accept()


# ------------------------------------------------------------------ CLASSIC (EASyPLUS)
# Every size is a fraction of the screen HEIGHT, so it looks the same at 768p, 1080p, 4K.
CLASSIC = {
    "top": 0.052, "bottom": 0.026,
    "header_px": 0.073, "header_gap": 0.039,
    "ticker_px": 0.0586, "ticker_h": 0.13,
    "mid_px": 0.0586, "mid_spacing": 0.117,
    "speed": 0.0039, "gap": 0.16,
}


class EasyPlusScreen(_BaseScreen):
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: black;")
        lay = QVBoxLayout(self)
        lay.setSpacing(0)
        self.header = QLabel("")
        self.header.setAlignment(Qt.AlignCenter)
        self.header.setStyleSheet("color: white; background: black;")
        self.ticker = MarqueeLabel()

        mid = QWidget()
        mid.setStyleSheet("background: black;")
        self.mid_layout = QVBoxLayout(mid)
        self.mid_layout.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self.mid_layout.setContentsMargins(0, 0, 0, 0)
        self.org_l, self.issued_l, self.type_l = QLabel(), QLabel(), QLabel()
        for l in (self.org_l, self.issued_l, self.type_l):
            l.setAlignment(Qt.AlignCenter)
            l.setStyleSheet("color: white; background: black;")
            l.setWordWrap(True)
            self.mid_layout.addWidget(l)
        lay.addWidget(self.header)
        lay.addWidget(self.ticker)
        lay.addWidget(mid, stretch=1)

    def _apply_scale(self):
        c, h, k = CLASSIC, max(self.height(), 480), _style["classic_scale"]
        self.layout().setContentsMargins(0, int(h * c["top"]), 0, int(h * c["bottom"]))
        self.header.setFont(font_px(h * c["header_px"] * k, True))
        self.header.setContentsMargins(0, 0, 0, int(h * c["header_gap"]))
        tpx = h * c["ticker_px"] * k
        self.ticker.setFixedHeight(int(h * c["ticker_h"] * k))
        self.ticker.configure(font_px(tpx, True), h * c["speed"], h * c["gap"])
        self.mid_layout.setSpacing(int(h * c["mid_spacing"] * k))
        for l in (self.org_l, self.issued_l, self.type_l):
            l.setFont(font_px(h * c["mid_px"] * k, True))

    def resizeEvent(self, e):
        self._apply_scale()
        super().resizeEvent(e)

    def show_record(self, rec, seconds=None):
        self.header.setText("NATIONAL ALERT" if rec.is_national else "EMERGENCY ALERT SYSTEM")
        self.org_l.setText(ORIGINATOR_NAMES.get(rec.alert.org, rec.alert.org))
        self.issued_l.setText(f"has issued {article_for(rec.title)}")
        self.type_l.setText(rec.title)
        place_on_screen(self)
        self._apply_scale()
        self.ticker.setText(f"{rec.text_classic or rec.text}     ")
        self.raise_()
        self.activateWindow()
        self.close_timer.start(int(seconds or _style["seconds"]) * 1000)


# ------------------------------------------------------------------ DETAILS CHANNEL (idle screens)
# What each box shows while no alert is on air - like a channel waiting for a video input to interrupt it.
# One painter per style (added as photos of the real units arrive); styles without one use the EASyPLUS look.
def _standby_easyplus(p, W, H, texts):
    p.fillRect(0, 0, W, H, Qt.black)
    p.setFont(font_px(H * 0.075 * _style["classic_scale"], False))
    p.setPen(QColor("white"))
    p.drawText(int(W * 0.185), int(H * 0.283), texts.get("easyplus") or "EAS DETAILS CHANNEL")


# EASyCAP: black surround, a blue panel, the system name across its top (measured from a photo of the unit).
EASYCAP_STANDBY = {"x0": 0.0545, "x1": 0.944, "y0": 0.237, "y1": 0.859,   # blue panel, fractions of the screen
                   "text_w": 0.86, "text_y": 0.077,                        # text width / centre, fractions of the panel
                   "blue": (0, 0, 190), "fg": (236, 236, 236)}


def _standby_easycap(p, W, H, texts):
    c = EASYCAP_STANDBY
    p.fillRect(0, 0, W, H, Qt.black)
    bx, by = W * c["x0"], H * c["y0"]
    bw, bh = W * (c["x1"] - c["x0"]), H * (c["y1"] - c["y0"])
    p.fillRect(QRectF(bx, by, bw, bh), QColor(*c["blue"]))
    label = "Emergency Alert System"
    fam = ["DejaVu Sans", "Verdana", "Arial", "Liberation Sans"]
    px = bh * 0.1
    fm = QFontMetrics(mkfont(fam, px, True))
    px *= (c["text_w"] * bw) / max(1, fm.horizontalAdvance(label))     # fit the text to the panel width like the unit
    p.setFont(mkfont(fam, px, True))
    p.setPen(QColor(*c["fg"]))
    th = px * 1.5
    text_c(p, label, by + bh * c["text_y"] - th / 2, th, W, bx, bw)


# DASDEC: bright green that darkens toward the bottom, black bars top and bottom, the unit's name across the top
# and the page counter at the bottom (measured from a photo of the unit).
DASDEC_STANDBY = {"y0": 0.0789, "y1": 0.960, "hold": 0.50, "top": (120, 255, 76), "bottom": (46, 112, 30),
                  "name_px": 0.0455, "name_y": 0.130, "name_fg": (242, 242, 242),
                  "count_px": 0.062, "count_y": 0.9246, "count_fg": (116, 116, 116)}


def _standby_dasdec(p, W, H, texts):
    c = DASDEC_STANDBY
    p.fillRect(0, 0, W, H, Qt.black)
    y0, y1 = H * c["y0"], H * c["y1"]
    g = QLinearGradient(0, y0, 0, y1)
    g.setColorAt(0.0, QColor(*c["top"]))
    g.setColorAt(c["hold"], QColor(*c["top"]))
    g.setColorAt(1.0, QColor(*c["bottom"]))
    p.fillRect(QRectF(0, y0, W, y1 - y0), g)
    fam = [_style["digital_font"] or "Courier New"] + MONO
    p.setFont(mkfont(fam, H * c["name_px"], False))
    p.setPen(QColor(*c["name_fg"]))
    th = H * c["name_px"] * 1.6
    text_c(p, texts.get("dasdec") or "DEERDEC Details Channel", H * c["name_y"] - th / 2, th, W)
    p.setFont(mkfont(fam, H * c["count_px"], False))
    p.setPen(QColor(*c["count_fg"]))
    th = H * c["count_px"] * 1.6
    text_c(p, "1/1", H * c["count_y"] - th / 2, th, W)


# VDS: plain blue screen, the system name and "DETAILS CHANNEL" in wide-spaced pixel caps near the top, cream with a
# dark outline (from a photo of the unit).
VDS_STANDBY = {"blue": (10, 30, 200), "px": 0.047, "y1": 0.205, "y2": 0.285, "fg": (240, 234, 205), "spacing": 125}


def _standby_vds(p, W, H, texts):
    c = VDS_STANDBY
    p.fillRect(0, 0, W, H, QColor(*c["blue"]))
    p.setFont(mkfont(["Arial", "Liberation Sans"], H * c["px"], True, spacing=c["spacing"]))
    th = H * c["px"] * 1.5
    d = max(2, int(H * 0.004))
    for key, y in (("EMERGENCY ALERT SYSTEM", c["y1"]), ((texts.get("vds") or "DETAILS CHANNEL").upper(), c["y2"])):
        text_c_shadow(p, key, H * y - th / 2, th, W, color=QColor(*c["fg"]), d=d)


# PSC-902: blue fading to navy toward the bottom with dark screen edges, "EMERGENCY / ALERT SYSTEM" in big bold
# spaced caps at the top (measured from a photo of the unit).
PSC902_STANDBY = {"top": (12, 28, 200), "bottom": (0, 0, 85), "px_h": 0.146, "max_w": 0.705, "spacing": 112,
                  "y1": 0.104, "y2": 0.246, "fg": (240, 240, 228), "edge": 0.012}


def _standby_psc902(p, W, H, texts):
    c = PSC902_STANDBY
    g = QLinearGradient(0, 0, 0, H)
    g.setColorAt(0.0, QColor(*c["top"]))
    g.setColorAt(1.0, QColor(*c["bottom"]))
    p.fillRect(QRectF(0, 0, W, H), g)
    e = W * c["edge"]
    for x0, x1 in ((0, e), (W, W - e)):
        eg = QLinearGradient(x0, 0, x1, 0)
        eg.setColorAt(0.0, QColor(0, 0, 0, 255))
        eg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(min(x0, x1), 0, e, H), eg)
    fam = ["Arial", "Liberation Sans"]
    px = H * c["px_h"]
    fm = QFontMetrics(mkfont(fam, px, True, spacing=c["spacing"]))
    px = min(px, px * (c["max_w"] * W) / max(1, fm.horizontalAdvance("ALERT SYSTEM")))
    p.setFont(mkfont(fam, px, True, spacing=c["spacing"]))
    th = px * 1.4
    d = max(2, int(H * 0.006))
    for line, y in (("EMERGENCY", c["y1"]), ("ALERT SYSTEM", c["y2"])):
        text_c_shadow(p, line, H * y - th / 2, th, W, color=QColor(*c["fg"]), d=d)


# Guessed idle screens (no photo of these units yet): each mirrors its own alert layout with the event/crawl left out.
def _standby_cems1000(p, W, H, texts):
    fh = min(H, W * 3 / 4)
    fw = fh * 4 / 3
    fx, fy = (W - fw) / 2, (H - fh) / 2
    p.fillRect(0, 0, W, H, QColor("black"))
    p.fillRect(QRectF(fx + fw * 0.04, fy + fh * 0.04, fw * 0.92, fh * 0.89), QColor(10, 10, 150))
    p.setPen(QColor(235, 235, 235))
    p.setFont(mkfont(MONO, fh * 0.07, False))
    text_c(p, "EAS Transmission", fy + fh * 0.07, fh * 0.1, fw, fx)
    p.setFont(mkfont(MONO, fh * 0.062, False))
    text_c(p, "DETAILS CHANNEL", fy + fh * 0.215, fh * 0.068, fw, fx)


def _standby_xfinity(p, W, H, texts):
    p.fillRect(0, 0, W, H, QColor("black"))
    p.setPen(QColor("white"))
    p.setFont(mkfont(SANS, H * 0.075, False))
    p.drawText(QRectF(W * 0.22, H * 0.36, W * 0.7, H * 0.09), Qt.AlignLeft | Qt.AlignVCenter, "EMERGENCY")
    p.drawText(QRectF(W * 0.22, H * 0.45, W * 0.7, H * 0.09), Qt.AlignLeft | Qt.AlignVCenter, "ALERT")


def _standby_atari800(p, W, H, texts):
    p.fillRect(0, 0, W, H, QColor("black"))
    u = max(1, int(min(H * 0.0092, W * 0.92 / (7 * 22))))
    for text, color, y in (("EMERGENCY ALERT SYSTEM", PEACH, H * 0.05), ("DETAILS CHANNEL", "white", H * 0.05 + 22 * u)):
        p.drawImage(QRectF((W - _bwidth(text, u)) / 2, y, (7 * len(text) - 2) * u, 7 * u), _bimg(text, color))


def _standby_crawl(p, W, H, texts):
    p.fillRect(0, 0, W, H, QColor(CRAWL["bg"]))


STANDBY = {"easyplus": _standby_easyplus, "easycap": _standby_easycap, "dasdec": _standby_dasdec,
           "vds": _standby_vds, "psc902": _standby_psc902, "cems1000": _standby_cems1000,
           "xfinity": _standby_xfinity, "atari800": _standby_atari800, "crawl": _standby_crawl}


class DetailsScreen(QWidget):
    """The Details Channel: kept up full-screen on the chosen monitor. Alerts open over it (they are always on
    top) and when they close this is what is left."""

    def __init__(self, texts=None, style="easyplus"):
        super().__init__()
        self.texts, self.style = dict(texts or {}), ALIASES.get(style, style)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("background-color: black;")

    def resizeEvent(self, e):
        self.update()                                 # repaint at the new size (resized / moved to another monitor)
        super().resizeEvent(e)

    def screenChanged(self, *_):
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        STANDBY.get(self.style, _standby_easyplus)(p, self.width(), self.height(), self.texts)


# ------------------------------------------------------------------ DIGITAL (DASDEC-II)
THEME = {
    "surround": "#000000", "panel": "#2c3351", "border": "#6e1f27", "text": "#ffffff",
    "font": "Courier New", "bold": True,
    "cols": 35, "cols_width_frac": 0.795, "lines_per_page": 11,
    "pitch_frac": 0.0645, "text_top_frac": 0.083, "counter_frac": 0.865,
    "panel_l": 0.088, "panel_r": 0.088, "panel_t": 0.053, "panel_b": 0.069,
    "border_frac": 0.012,
}


class DasdecScreen(_BaseScreen):
    def __init__(self, page_seconds=8.0):
        super().__init__()
        self.page_seconds = page_seconds
        self.pages, self.idx = [[""]], 0
        self.page_timer = QTimer(self)
        self.page_timer.timeout.connect(self._next)

    def show_record(self, rec, seconds=None):
        from .layout import paginate
        t = THEME
        self.pages = paginate([rec.paras or [rec.text], rec.extra], t["cols"], t["lines_per_page"])
        self.idx = 0
        place_on_screen(self)
        self.raise_()
        self.activateWindow()
        self.page_timer.start(int(self.page_seconds * 1000))
        self.close_timer.start(int(seconds or _style["seconds"]) * 1000)
        self.update()

    def _next(self):
        self.idx = (self.idx + 1) % len(self.pages)
        self.update()

    def hideEvent(self, e):
        self.page_timer.stop()
        super().hideEvent(e)

    def _font(self, fw):
        t = THEME
        f = QFont(_style.get("digital_font") or t["font"])
        f.setStyleHint(QFont.TypeWriter)
        f.setBold(t["bold"])
        f.setPixelSize(20)
        adv = QFontMetrics(f).horizontalAdvance("M") or 12
        f.setPixelSize(max(8, int(20 * (t["cols_width_frac"] * fw / t["cols"]) / adv)))
        return f

    def paintEvent(self, _):
        t, W, H = THEME, self.width(), self.height()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(t["surround"]))
        fh = min(H, W * 3 / 4)
        fw = fh * 4 / 3
        fx, fy = (W - fw) / 2, (H - fh) / 2
        px, py = fx + fw * t["panel_l"], fy + fh * t["panel_t"]
        pw = fw * (1 - t["panel_l"] - t["panel_r"])
        ph = fh * (1 - t["panel_t"] - t["panel_b"])
        p.fillRect(int(px), int(py), int(pw), int(ph), QColor(t["border"]))
        b = max(2, int(fw * t["border_frac"]))
        p.fillRect(int(px + b), int(py + b), int(pw - 2 * b), int(ph - 2 * b), QColor(t["panel"]))
        p.setFont(self._font(fw))
        p.setPen(QColor(t["text"]))
        pitch = fh * t["pitch_frac"]
        y = fy + fh * t["text_top_frac"]
        for line in self.pages[self.idx]:
            p.drawText(int(fx), int(y), int(fw), int(pitch), Qt.AlignHCenter | Qt.AlignVCenter, line)
            y += pitch
        p.drawText(int(fx), int(fy + fh * t["counter_frac"]), int(fw), int(pitch),
                   Qt.AlignHCenter | Qt.AlignVCenter, f"{self.idx + 1}/{len(self.pages)}")

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Right, Qt.Key_Space):
            self._next()
        else:
            super().keyPressEvent(e)


# ------------------------------------------------------------------ CRAWL (generic, colored band)
CRAWL = {
    "advisory": ("#0b8f2e", "#ffffff"),     # green  - tests / advisories / statements
    "watch":    ("#f2c500", "#000000"),     # yellow - watches
    "alert":    ("#c40000", "#ffffff"),     # red    - warnings / emergencies / alerts
    "position": "bottom",                   # top | middle | bottom
    "band_h": 0.16,                         # band height (fraction of screen height)
    "text_px": 0.085,                       # text size (fraction of screen height)
    "margin": 0.04,
    "speed": 0.0045,
    "bg": "#000000",
}


class CrawlScreen(_BaseScreen):
    """Just a crawl. Base for the lookalike CGENs (VDS, Onics, CEMS-1000, ...)."""

    def __init__(self):
        super().__init__()
        self.setStyleSheet(f"background-color: {CRAWL['bg']};")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.ticker = MarqueeLabel()
        self.top, self.bot = QWidget(), QWidget()
        self.lay.addWidget(self.top)
        self.lay.addWidget(self.ticker)
        self.lay.addWidget(self.bot)

    def _apply(self, level):
        h = max(self.height(), 480)
        bg, fg = CRAWL.get(level, CRAWL["alert"])
        self.ticker.bg, self.ticker.fg = bg, fg
        bh = int(h * CRAWL["band_h"])
        self.ticker.setFixedHeight(bh)
        self.ticker.configure(font_px(h * CRAWL["text_px"], True, family=_style["custom"] or _style["family"]),
                              h * CRAWL["speed"], h * 0.2)
        m, pos = int(h * CRAWL["margin"]), CRAWL["position"]
        self.lay.setStretch(0, 0 if pos == "top" else 1)       # space above the band
        self.lay.setStretch(2, 0 if pos == "bottom" else 1)    # space below the band
        self.top.setMinimumHeight(m if pos == "top" else 0)
        self.bot.setMinimumHeight(m if pos == "bottom" else 0)

    def show_record(self, rec, seconds=None):
        place_on_screen(self)
        self._apply(rec.level)
        self.ticker.setText(f"{rec.text_classic or rec.text}     ")
        self.raise_()
        self.activateWindow()
        self.close_timer.start(int(seconds or _style["seconds"]) * 1000)


# ------------------------------------------------------------------ painter helpers
PEACH = "#f2c9a2"
LEVEL_BRIGHT = {"advisory": "#3dff6a", "watch": "#ffd400", "alert": "#ff4a3d"}
LEVEL_PEACH = {"advisory": "#2fb34a", "watch": "#f2c500", "alert": "#e0392b"}
ORG_LONG = {
    "PEP": "The Primary Entry Point System",
    "CIV": "A civil authority",
    "WXR": "The National Weather Service",
    "EAS": "A Broadcast station or cable system",
}
SANS = ["Arial", "Liberation Sans", "Helvetica", "DejaVu Sans"]
MONO = ["Lucida Console", "Consolas", "DejaVu Sans Mono", "Courier New"]


_FONT_CACHE, _AVAIL = {}, None


def mkfont(families, px, bold=True, aa=True, spacing=None):
    """Cached: the screens call this every frame, and listing installed fonts is slow."""
    global _AVAIL
    key = (tuple(families), int(px), bold, aa, spacing)
    f = _FONT_CACHE.get(key)
    if f is None:
        if _AVAIL is None:
            _AVAIL = set(QFontDatabase.families())
        fam = next((x for x in families if x and x in _AVAIL), [x for x in families if x][-1])
        f = QFont(fam)
        f.setPixelSize(max(8, int(px)))
        f.setBold(bold)
        if not aa:
            f.setStyleStrategy(QFont.NoAntialias)
        if spacing:
            f.setLetterSpacing(QFont.PercentageSpacing, spacing)
        _FONT_CACHE[key] = f
    return f


class CrawlStrip:
    """A line of text rendered ONCE into pixmap tiles. Scrolling then only blits pixmaps,
    which is what keeps crawls smooth at any resolution."""
    TILE = 3000

    def __init__(self, text, font, color, h, dpr=1.0, shadow=None):
        fm = QFontMetrics(font)
        pad = int(max(2, h * 0.03)) + 2 if shadow else 2
        segs, cur = [], ""
        for w in text.split(" "):
            nxt = f"{cur}{w} "
            if cur and fm.horizontalAdvance(nxt) > self.TILE:
                segs.append(cur)
                cur = f"{w} "
            else:
                cur = nxt
        if cur:
            segs.append(cur)
        self.tiles, x = [], 0
        for seg in segs:
            sw = fm.horizontalAdvance(seg)
            pm = QPixmap(int((sw + pad * 2) * dpr) + 1, int(h * dpr) + 1)
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            q = QPainter(pm)
            q.setFont(font)
            r = QRectF(pad, 0, sw + pad, h)
            if shadow:
                q.setPen(QColor(shadow))
                q.drawText(r.translated(pad - 1, pad - 1), Qt.AlignLeft | Qt.AlignVCenter, seg)
            q.setPen(QColor(color))
            q.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, seg)
            q.end()
            self.tiles.append((x - pad, sw, pm))
            x += sw
        self.w = x

    def draw(self, p, x0, top, left, right):
        for off, sw, pm in self.tiles:
            x = int(x0 + off)
            if x > right or x + sw + 8 < left:
                continue
            p.drawPixmap(x, int(top), pm)


_STRIPS = {}


def crawl_strip(p, text, font, color, h, shadow=None):
    dev = p.device() if hasattr(p, "device") else p       # QPainter -> its device; QWidget -> itself
    dpr = dev.devicePixelRatioF() if hasattr(dev, "devicePixelRatioF") else 1.0
    key = (text, font.key(), str(color), int(h), round(dpr, 2), shadow)
    st = _STRIPS.get(key)
    if st is None:
        if len(_STRIPS) > 4:
            _STRIPS.clear()
        st = _STRIPS[key] = CrawlStrip(text, font, color, h, dpr, shadow)
    return st


def wrap_px(text, fm, width):
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for w in para.split():
            t = f"{cur} {w}".strip()
            if not cur or fm.horizontalAdvance(t) <= width:
                cur = t
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def chunks(lines, n):
    return [lines[i:i + n] for i in range(0, len(lines), n)] or [[""]]


def text_c(p, s, y, h, W, x0=0.0, w=None):
    p.drawText(QRectF(x0, y, w if w is not None else W, h), Qt.AlignHCenter | Qt.AlignVCenter, s)


def text_c_shadow(p, s, y, h, W, color="white", shadow="black", d=None):
    """Centered text with a hard black drop shadow (down-right)."""
    d = d if d is not None else max(2, int(h * 0.05))
    pen = p.pen()
    p.setPen(QColor(shadow))
    p.drawText(QRectF(d, y + d, W, h), Qt.AlignHCenter | Qt.AlignVCenter, s)
    p.setPen(QColor(color))
    p.drawText(QRectF(0, y, W, h), Qt.AlignHCenter | Qt.AlignVCenter, s)
    p.setPen(pen)


def draw_crawl(p, text, rect, font, color, speed, ms, shadow=None):
    """Right-to-left crawl; position comes from elapsed time, so speed is steady."""
    st = crawl_strip(p, text, font, color, rect.height(), shadow)
    period = st.w + int(rect.height() * 2)
    base = rect.right() - (ms / 1000.0 * speed) % period
    p.save()
    p.setClipRect(rect)
    for k in range(-(int(rect.width() / period) + 2), 2):
        st.draw(p, base + k * period, rect.top(), rect.left(), rect.right())
    p.restore()


class PaintScreen(_BaseScreen):
    """Self-painting screen: timer drives page flips, blinking and crawling.
    Subclasses implement build_pages(W, H) -> list and draw(p, W, H, page, ms)."""

    def __init__(self, page_seconds=8.0):
        super().__init__()
        self.page_seconds = max(1.0, float(page_seconds))
        self.rec, self.pages, self.npages, self.page, self._key = None, [None], 1, 0, None
        self.clock = QElapsedTimer()
        self.tick = QTimer(self)
        self.tick.setTimerType(Qt.PreciseTimer)
        self.tick.timeout.connect(self._tick)

    def build_pages(self, W, H):
        return [None]

    def draw(self, p, W, H, page, ms):
        pass

    def show_record(self, rec, seconds=None):
        self.rec, self._key, self.page = rec, None, 0
        self.clock.start()
        place_on_screen(self)
        self.raise_()
        self.activateWindow()
        self.tick.start(16)
        self.close_timer.start(int(seconds or _style["seconds"]) * 1000)

    def hideEvent(self, e):
        self.tick.stop()
        super().hideEvent(e)

    def _tick(self):
        self.page = int(self.clock.elapsed() / (self.page_seconds * 1000)) % max(1, self.npages)
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Right, Qt.Key_Space) and self.npages > 1:
            self.clock.start()
            self.page = (self.page + 1) % self.npages
            self.update()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _):
        if not self.rec:
            return
        W, H = self.width(), self.height()
        key = (W, H, id(self.rec))
        if key != self._key:
            self._key = key
            self.pages = self.build_pages(W, H) or [None]
            self.npages = len(self.pages)
        p = QPainter(self)
        self.draw(p, W, H, self.pages[self.page % self.npages], self.clock.elapsed())


# ------------------------------------------------------------------ Atari 800XL (black, pixel text, blinking event)
ATARI_FONT = {
    'A': ('.XXX.', 'X...X', 'X...X', 'XXXXX', 'X...X', 'X...X', 'X...X'),
    'B': ('XXXX.', 'X...X', 'X...X', 'XXXX.', 'X...X', 'X...X', 'XXXX.'),
    'C': ('.XXXX', 'X....', 'X....', 'X....', 'X....', 'X....', '.XXXX'),
    'D': ('XXXX.', 'X...X', 'X...X', 'X...X', 'X...X', 'X...X', 'XXXX.'),
    'E': ('XXXXX', 'X....', 'X....', 'XXXX.', 'X....', 'X....', 'XXXXX'),
    'F': ('XXXXX', 'X....', 'X....', 'XXXX.', 'X....', 'X....', 'X....'),
    'G': ('.XXXX', 'X....', 'X....', 'X.XXX', 'X...X', 'X...X', '.XXXX'),
    'H': ('X...X', 'X...X', 'X...X', 'XXXXX', 'X...X', 'X...X', 'X...X'),
    'I': ('XXXXX', '..X..', '..X..', '..X..', '..X..', '..X..', 'XXXXX'),
    'J': ('..XXX', '...X.', '...X.', '...X.', '...X.', 'X..X.', '.XX..'),
    'K': ('X...X', 'X..X.', 'X.X..', 'XX...', 'X.X..', 'X..X.', 'X...X'),
    'L': ('X....', 'X....', 'X....', 'X....', 'X....', 'X....', 'XXXXX'),
    'M': ('X...X', 'XX.XX', 'X.X.X', 'X.X.X', 'X...X', 'X...X', 'X...X'),
    'N': ('X...X', 'XX..X', 'X.X.X', 'X..XX', 'X...X', 'X...X', 'X...X'),
    'O': ('.XXX.', 'X...X', 'X...X', 'X...X', 'X...X', 'X...X', '.XXX.'),
    'P': ('XXXX.', 'X...X', 'X...X', 'XXXX.', 'X....', 'X....', 'X....'),
    'Q': ('.XXX.', 'X...X', 'X...X', 'X...X', 'X.X.X', 'X..X.', '.XX.X'),
    'R': ('XXXX.', 'X...X', 'X...X', 'XXXX.', 'X.X..', 'X..X.', 'X...X'),
    'S': ('.XXXX', 'X....', 'X....', '.XXX.', '....X', '....X', 'XXXX.'),
    'T': ('XXXXX', '..X..', '..X..', '..X..', '..X..', '..X..', '..X..'),
    'U': ('X...X', 'X...X', 'X...X', 'X...X', 'X...X', 'X...X', '.XXX.'),
    'V': ('X...X', 'X...X', 'X...X', 'X...X', 'X...X', '.X.X.', '..X..'),
    'W': ('X...X', 'X...X', 'X...X', 'X.X.X', 'X.X.X', 'XX.XX', 'X...X'),
    'X': ('X...X', 'X...X', '.X.X.', '..X..', '.X.X.', 'X...X', 'X...X'),
    'Y': ('X...X', 'X...X', '.X.X.', '..X..', '..X..', '..X..', '..X..'),
    'Z': ('XXXXX', '....X', '...X.', '..X..', '.X...', 'X....', 'XXXXX'),
    '0': ('.XXX.', 'X...X', 'X..XX', 'X.X.X', 'XX..X', 'X...X', '.XXX.'),
    '1': ('..X..', '.XX..', '..X..', '..X..', '..X..', '..X..', '.XXX.'),
    '2': ('.XXX.', 'X...X', '....X', '...X.', '..X..', '.X...', 'XXXXX'),
    '3': ('XXXX.', '....X', '....X', '.XXX.', '....X', '....X', 'XXXX.'),
    '4': ('...X.', '..XX.', '.X.X.', 'X..X.', 'XXXXX', '...X.', '...X.'),
    '5': ('XXXXX', 'X....', 'XXXX.', '....X', '....X', 'X...X', '.XXX.'),
    '6': ('..XX.', '.X...', 'X....', 'XXXX.', 'X...X', 'X...X', '.XXX.'),
    '7': ('XXXXX', '....X', '...X.', '..X..', '.X...', '.X...', '.X...'),
    '8': ('.XXX.', 'X...X', 'X...X', '.XXX.', 'X...X', 'X...X', '.XXX.'),
    '9': ('.XXX.', 'X...X', 'X...X', '.XXXX', '....X', '...X.', '.XX..'),
    ' ': ('.....', '.....', '.....', '.....', '.....', '.....', '.....'),
    '.': ('.....', '.....', '.....', '.....', '.....', '.XX..', '.XX..'),
    ',': ('.....', '.....', '.....', '.....', '.XX..', '..X..', '.X...'),
    ':': ('.....', '.XX..', '.XX..', '.....', '.XX..', '.XX..', '.....'),
    ';': ('.....', '.XX..', '.XX..', '.....', '.XX..', '..X..', '.X...'),
    '-': ('.....', '.....', '.....', 'XXXXX', '.....', '.....', '.....'),
    "'": ('..X..', '..X..', '.X...', '.....', '.....', '.....', '.....'),
    '/': ('....X', '....X', '...X.', '..X..', '.X...', 'X....', 'X....'),
    '!': ('..X..', '..X..', '..X..', '..X..', '..X..', '.....', '..X..'),
    '?': ('.XXX.', 'X...X', '....X', '...X.', '..X..', '.....', '..X..'),
    '(': ('...X.', '..X..', '.X...', '.X...', '.X...', '..X..', '...X.'),
    ')': ('.X...', '..X..', '...X.', '...X.', '...X.', '..X..', '.X...'),
    '&': ('.XX..', 'X..X.', 'X.X..', '.X...', 'X.X.X', 'X..X.', '.XX.X'),
    '+': ('.....', '..X..', '..X..', 'XXXXX', '..X..', '..X..', '.....'),
    '%': ('XX..X', 'XX..X', '...X.', '..X..', '.X...', 'X..XX', 'X..XX'),
}


def _bwidth(text, u):
    """Pixel width of `text` in the built-in Atari font at `u` screen pixels per font pixel."""
    return max(0, 7 * len(text) - 2) * u


def _bwrap(text, u, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = f"{cur} {w}".strip()
        if not cur or _bwidth(t, u) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


_BIMG = {}


def _bimg(text, color):
    """The text as a tiny image, one pixel per font pixel (scaled up with hard edges when drawn)."""
    key = (text, str(color))
    im = _BIMG.get(key)
    if im is None:
        if len(_BIMG) > 40:
            _BIMG.clear()
        n = max(1, 7 * len(text) - 2)
        im = QImage(n, 7, QImage.Format_ARGB32)
        im.fill(0)
        rgba = QColor(color).rgba()
        for i, ch in enumerate(text):
            for y, row in enumerate(ATARI_FONT.get(ch, ATARI_FONT["?"])):
                for x, c in enumerate(row):
                    if c == "X" and 7 * i + x < n:
                        im.setPixel(7 * i + x, y, rgba)
        _BIMG[key] = im
    return im


class Atari800Screen(PaintScreen):
    """Built-in blocky Atari-style font drawn from pixel data: no font files, scales to any screen."""
    ON_MS, OFF_MS = 1000, 600            # slow blink: event text visible 1.0 s, hidden 0.6 s

    @staticmethod
    def _put(p, text, color, x, y, u):
        if text:
            p.drawImage(QRectF(x, y, (7 * len(text) - 2) * u, 7 * u), _bimg(text, color))

    def _center(self, p, text, color, W, y, u):
        self._put(p, text, color, (W - _bwidth(text, u)) / 2, y, u)

    def draw(self, p, W, H, page, ms):
        if _style.get("atari"):
            self._draw_font(p, W, H, ms)
        else:
            self._draw_bitmap(p, W, H, ms)

    def _draw_font(self, p, W, H, ms):
        """fonts/atari.ttf: an 8x8-pixel font (1024 units/em), so sizes that are multiples of 8 px
        give perfectly square, crisp pixels at any resolution."""
        r = self.rec
        p.fillRect(0, 0, W, H, QColor("black"))
        title = r.title.upper()
        sc = max(0.3, min(1.6, _style.get("atari_scale", 1.0)))
        P = 8 * max(1, int(min(H * 0.060 * sc, W * 0.92 / 22) / 8))      # header (22 chars) must fit
        while True:
            f = mkfont([_style["atari"]], P, False, aa=False)
            fm = QFontMetrics(f)
            lines = wrap_px(title, fm, W * 0.94)
            lp = P * 1.5
            y_ev = H * 0.05 + lp * 2
            y_rest = y_ev + lp * len(lines) + lp * 1.2
            if y_rest + lp * 2.6 + P <= H * 0.96 or P <= 8:
                break
            P -= 8
        p.setFont(f)
        asc = fm.ascent()

        def put(text, color, x, y):
            p.setPen(QColor(color))
            p.drawText(QPointF(round(x), round(y + asc)), text)

        def center(text, color, y):
            put(text, color, (W - fm.horizontalAdvance(text)) / 2, y)

        center("EMERGENCY ALERT SYSTEM", PEACH, H * 0.05)
        if ms % (self.ON_MS + self.OFF_MS) < self.ON_MS:                 # the blinking part
            col = LEVEL_PEACH.get(r.level, LEVEL_PEACH["alert"])
            for i, ln in enumerate(lines):
                center(ln, col, y_ev + i * lp)
        center("FOR YOUR AREA", "white", y_rest)
        a, b = "UNTIL ", f"{r.end_local.upper()}."
        wa, wb = fm.horizontalAdvance(a), fm.horizontalAdvance(b)
        x = (W - wa - wb) / 2
        put(a, "white", x, y_rest + lp)
        put(b, PEACH, x + wa, y_rest + lp)
        center((r.station or "").upper(), PEACH, y_rest + lp * 2.6)

    def _draw_bitmap(self, p, W, H, ms):
        """Fallback when fonts/atari.ttf is missing: the built-in blocky font."""
        r = self.rec
        p.fillRect(0, 0, W, H, QColor("black"))
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)       # hard pixel edges
        title = r.title.upper()
        sc = max(0.3, min(1.6, _style.get("atari_scale", 1.0)))
        u = max(1, int(min(H * 0.0092 * sc, W * 0.92 / (7 * 22))))   # header (22 chars) must fit
        while True:                                                  # shrink until everything fits
            lines = _bwrap(title, u, W * 0.94)
            lp = 11 * u
            y_ev = H * 0.05 + lp * 2
            y_rest = y_ev + lp * len(lines) + lp * 1.2
            if y_rest + lp * 2.6 + 7 * u <= H * 0.96 or u == 1:
                break
            u -= 1
        self._center(p, "EMERGENCY ALERT SYSTEM", PEACH, W, H * 0.05, u)
        if ms % (self.ON_MS + self.OFF_MS) < self.ON_MS:             # the blinking part
            col = LEVEL_PEACH.get(r.level, LEVEL_PEACH["alert"])
            for i, ln in enumerate(lines):
                self._center(p, ln, col, W, y_ev + i * lp, u)
        self._center(p, "FOR YOUR AREA", "white", W, y_rest, u)
        a, b = "UNTIL ", f"{r.end_local.upper()}."
        wa, wb = 7 * len(a) * u, _bwidth(b, u)
        x = (W - wa - wb) / 2
        self._put(p, a, "white", x, y_rest + lp, u)
        self._put(p, b, PEACH, x + wa, y_rest + lp, u)
        self._center(p, (r.station or "").upper(), PEACH, W, y_rest + lp * 2.6, u)


# ------------------------------------------------------------------ EASyCAP (blue, paged body)
class EasyCapScreen(PaintScreen):
    PER_PAGE = 6

    def build_pages(self, W, H):
        fm = QFontMetrics(mkfont(SANS, H * 0.062))
        body = " ".join(x for x in (self.rec.text_classic, self.rec.message) if x)
        return chunks(wrap_px(body, fm, W * 0.94), self.PER_PAGE)

    def draw(self, p, W, H, page, ms):
        r = self.rec
        p.fillRect(0, 0, W, H, QColor(0, 0, 235))
        p.setPen(QColor("white"))
        p.setFont(mkfont(SANS, H * 0.072))
        text_c(p, "Emergency Alert System", H * 0.05, H * 0.1, W)
        text_c(p, r.title, H * 0.15, H * 0.1, W)
        p.setFont(mkfont(SANS, H * 0.062))
        y = H * 0.3
        for ln in page:
            text_c(p, ln, y, H * 0.067, W)
            y += H * 0.067
        p.setFont(mkfont(SANS, H * 0.05))
        text_c(p, f"- {self.page + 1} of {self.npages} -", H * 0.76, H * 0.08, W)


# ------------------------------------------------------------------ VDS (green)
class VdsScreen(PaintScreen):
    GREEN = QColor(47, 111, 38)

    def _font(self, H, k=0.062):
        return mkfont(SANS, H * k, True, spacing=112)

    def draw(self, p, W, H, page, ms):
        r = self.rec
        p.fillRect(0, 0, W, H, self.GREEN)
        p.setPen(QColor("white"))
        f = self._font(H)
        fm = QFontMetrics(f)
        p.setFont(f)
        text_c(p, "Emergency Alert System", H * 0.05, H * 0.12, W)
        y = H * 0.40
        for ln in wrap_px(r.title, fm, W * 0.9):
            text_c(p, ln, y, H * 0.1, W)
            y += H * 0.1
        # the full message crawls along the bottom, right to left
        body = " ".join(x for x in (r.text_classic or r.text, r.message) if x)
        draw_crawl(p, body + "          ", QRectF(0, H * 0.80, W, H * 0.12), f, "white", W * 0.16, ms)


# ------------------------------------------------------------------ CEMS-1000 (blue 4:3, left-aligned lines)
class CemsScreen(PaintScreen):
    PER_PAGE = 8

    def _frame(self, W, H):
        fh = min(H, W * 3 / 4)
        fw = fh * 4 / 3
        return (W - fw) / 2, (H - fh) / 2, fw, fh

    def build_pages(self, W, H):
        r = self.rec
        _x, _y, fw, fh = self._frame(W, H)
        fm = QFontMetrics(mkfont(MONO, fh * 0.062, False))
        lines = [" ALERT ALERT ALERT"] + wrap_px(r.title.upper(), fm, fw * 0.7)
        lines += ["COUNTIES INVOLVED:"] + [a.upper() for a in r.areas]
        lines += ["MESSAGE EXPIRES AT:", f"{r.end24} LOCAL"]
        return chunks(lines, self.PER_PAGE)

    def draw(self, p, W, H, page, ms):
        fx, fy, fw, fh = self._frame(W, H)
        p.fillRect(0, 0, W, H, QColor("black"))
        p.fillRect(QRectF(fx + fw * 0.04, fy + fh * 0.04, fw * 0.92, fh * 0.89), QColor(10, 10, 150))
        p.setPen(QColor(235, 235, 235))
        p.setFont(mkfont(MONO, fh * 0.07, False))
        text_c(p, "EAS Transmission", fy + fh * 0.07, fh * 0.1, fw, fx)
        p.setFont(mkfont(MONO, fh * 0.062, False))
        y = fy + fh * 0.215
        for ln in page:
            p.drawText(QRectF(fx + fw * 0.13, y, fw * 0.85, fh * 0.068),
                       Qt.AlignLeft | Qt.AlignVCenter, ln)
            y += fh * 0.068


# ------------------------------------------------------------------ Xfinity (black + colored crawl band)
class XfinityScreen(PaintScreen):
    BAND = {"alert": ((120, 20, 15), (205, 60, 42), "#e6ded2"),
            "watch": ((130, 100, 0), (225, 180, 0), "#000000"),
            "advisory": ((20, 90, 40), (46, 160, 70), "#e6ded2")}

    def draw(self, p, W, H, page, ms):
        r = self.rec
        p.fillRect(0, 0, W, H, QColor("black"))
        p.setPen(QColor("white"))
        p.setFont(mkfont(SANS, H * 0.075, False))
        p.drawText(QRectF(W * 0.22, H * 0.36, W * 0.7, H * 0.09), Qt.AlignLeft | Qt.AlignVCenter, "EMERGENCY")
        p.drawText(QRectF(W * 0.22, H * 0.45, W * 0.7, H * 0.09), Qt.AlignLeft | Qt.AlignVCenter, "ALERT")
        dark, bright, fg = self.BAND.get(r.level, self.BAND["alert"])
        band = QRectF(0, H * 0.66, W, H * 0.3)
        g = QLinearGradient(0, 0, W, 0)
        g.setColorAt(0, QColor(*dark))
        g.setColorAt(0.5, QColor(*bright))
        g.setColorAt(1, QColor(*dark))
        p.fillRect(band, g)
        draw_crawl(p, r.text_classic or r.text, band,
                   mkfont(["Liberation Mono", "Courier New", "Consolas"], H * 0.1, False), fg, W * 0.22, ms)


# ------------------------------------------------------------------ PSC-902 (blue, big event text + crawl band)
class Psc902Screen(PaintScreen):
    def draw(self, p, W, H, page, ms):
        r = self.rec
        p.fillRect(0, 0, W, H, QColor(10, 30, 200))
        fams = ["Arial", "Liberation Sans"]
        p.setPen(QColor("white"))
        p.setFont(mkfont(fams, H * 0.045, True, spacing=115))
        text_c_shadow(p, "EMERGENCY ALERT SYSTEM", H * 0.12, H * 0.07, W, d=max(2, int(H * 0.004)))
        # the event text must fit between the header and the crawl band: shrink until it does
        top, bottom = H * 0.24, H * 0.635
        px = H * 0.105
        while True:
            f = mkfont(fams, px, True)
            fm = QFontMetrics(f)
            lines = wrap_px(r.title.upper(), fm, W * 0.86)
            lh = px * 1.3
            if len(lines) * lh <= bottom - top or px <= H * 0.03:
                break
            px *= 0.93
        p.setFont(f)
        p.setPen(QColor("white"))
        y = top + ((bottom - top) - len(lines) * lh) / 2          # centred in the free space
        for ln in lines:
            text_c_shadow(p, ln, y, lh, W, d=max(2, int(H * 0.006)))
            y += lh
        band = QRectF(0, H * 0.665, W, H * 0.135)
        g = QLinearGradient(0, band.top(), 0, band.bottom())
        g.setColorAt(0, QColor(150, 50, 15))
        g.setColorAt(0.5, QColor(205, 80, 30))
        g.setColorAt(1, QColor(150, 50, 15))
        p.fillRect(band, g)
        draw_crawl(p, (r.text_classic or r.text).upper(), band,
                   mkfont(["Arial", "Liberation Sans"], H * 0.105, True), "white", W * 0.28, ms, shadow="black")


# ------------------------------------------------------------------ WEATHER BULLETIN (fictional station-style bulletin)
# A made-up local-TV weather bulletin card: blue field, a station logo plate, "WEATHER BULLETIN" and the event
# blinking in a warm color. The station name is set in Config (call letters + channel number).
BULLETIN = {"blue": (28, 52, 168), "plate": (82, 86, 92), "box": (214, 214, 210), "fg": (238, 238, 232),
            "ink": (30, 30, 34), "blink_on": 1000, "blink_off": 600,
            "event": {"alert": (238, 48, 40), "watch": (232, 190, 40), "advisory": (70, 190, 100),
                      "national": (255, 40, 40), "clear": (70, 190, 100)},
            "head": {"alert": "WEATHER BULLETIN", "watch": "WEATHER WATCH", "advisory": "WEATHER ADVISORY",
                     "civil": "CIVIL ALERT", "national": "NATIONAL ALERT", "clear": "ALL CLEAR"},
            # non-weather (civil) event codes get the CIVIL ALERT heading; EAN is the national alert
            "civil": {"CDW", "CEM", "CAE", "LEW", "LAE", "EVI", "HMW", "NUW", "RHW", "SPW", "TOE", "FRW", "ADR",
                      "NMN"}}


def _bulletin_kind(r):
    """Which heading/color a record gets: national (EAN), civil (non-weather), or its weather level."""
    code = (getattr(getattr(r, "alert", None), "eee", "") or "").upper()
    if code == "EAT":
        return "clear"
    if code == "EAN" or getattr(r, "is_national", False):
        return "national"
    if code in BULLETIN["civil"]:
        return "civil"
    return r.level if r.level in ("alert", "watch", "advisory") else "alert"


def _bulletin_station():
    parts = str(_style.get("bulletin") or "KDEER 12").upper().split()
    if len(parts) >= 2 and parts[-1].isdigit():
        return " ".join(parts[:-1]), parts[-1]
    return " ".join(parts) or "KDEER", ""


def _bulletin_base(p, W, H, head, national=False):
    c = BULLETIN
    p.fillRect(0, 0, W, H, QColor(120, 8, 8) if national else QColor(*c["blue"]))
    call, num = _bulletin_station()
    fam = ["Arial", "Liberation Sans", "Helvetica"]
    px = H * 0.115
    f = mkfont(fam, px, True)
    fm = QFontMetrics(f)
    cw, nw = fm.horizontalAdvance(call), (fm.horizontalAdvance(num) if num else 0)
    padx, bh = px * 0.45, px * 1.35
    total = cw + padx * 2 + (nw + padx * 1.2 if num else 0)
    scale = min(1.0, (W * 0.62) / max(1, total))
    if scale < 1.0:
        px *= scale
        f = mkfont(fam, px, True)
        fm = QFontMetrics(f)
        cw, nw = fm.horizontalAdvance(call), (fm.horizontalAdvance(num) if num else 0)
        padx, bh = px * 0.45, px * 1.35
        total = cw + padx * 2 + (nw + padx * 1.2 if num else 0)
    x, y = (W - total) / 2, H * 0.10
    plate = QRectF(x, y + bh * 0.08, cw + padx * 2, bh * 0.84)
    g = QLinearGradient(0, plate.top(), 0, plate.bottom())
    g.setColorAt(0, QColor(100, 104, 110))
    g.setColorAt(1, QColor(*c["plate"]))
    p.fillRect(plate, g)
    p.setFont(f)
    p.setPen(QColor(*c["fg"]))
    p.drawText(plate, Qt.AlignCenter, call)
    if num:
        box = QRectF(plate.right() + padx * 0.2, y - bh * 0.06, nw + padx * 1.0, bh * 1.12)
        p.fillRect(box, QColor(*c["box"]))
        p.setPen(QColor(*c["ink"]))
        p.drawText(box, Qt.AlignCenter, num)
    p.setPen(QColor(*c["fg"]))
    p.setFont(mkfont(fam, H * 0.082, True, spacing=104))
    lines = head.split(" ", 1) if " " in head else [head]
    ty = H * 0.34
    for ln in lines:
        text_c_shadow(p, ln, ty, H * 0.1, W, color=QColor(*c["fg"]), shadow="#10183a", d=max(1, int(H * 0.004)))
        ty += H * 0.1


def _standby_bulletin(p, W, H, texts):
    _bulletin_base(p, W, H, BULLETIN["head"]["alert"])


STANDBY["bulletin"] = _standby_bulletin


class BulletinScreen(PaintScreen):
    def draw(self, p, W, H, page, ms):
        r = self.rec
        lvl = _bulletin_kind(r)
        _bulletin_base(p, W, H, BULLETIN["head"][lvl], lvl == "national")
        if ms % (BULLETIN["blink_on"] + BULLETIN["blink_off"]) >= BULLETIN["blink_on"]:
            return                                              # the event text blinks
        fam = ["Arial", "Liberation Sans", "Helvetica"]
        px, top, bottom = H * 0.092, H * 0.62, H * 0.92
        while True:
            f = mkfont(fam, px, True)
            lines = wrap_px(r.title.upper(), QFontMetrics(f), W * 0.88)
            lh = px * 1.25
            if (len(lines) <= 2 and len(lines) * lh <= bottom - top) or px <= H * 0.03:
                break
            px *= 0.93
        p.setFont(f)
        y = top + ((bottom - top) - len(lines) * lh) / 2
        for ln in lines:
            text_c_shadow(p, ln, y, lh, W, color=QColor(*BULLETIN["event"].get(lvl, BULLETIN["event"]["alert"])), shadow="#10183a",
                          d=max(2, int(H * 0.005)))
            y += lh


# ------------------------------------------------------------------ DEERDEC (our own style)
# Dark forest-green screen. Slide 1: the event big, when it ends. Further slides: the alert's own message text
# (CAP text) paged like slides. The AREAS AFFECTED strip stays on screen the whole time.
DEER = {"bg_top": (12, 36, 28), "bg_bot": (4, 14, 12), "fg": (236, 240, 232), "dim": (150, 176, 160),
        "amber": (232, 176, 52), "max_msg_pages": 10, "max_area_lines": 4}


def _deer_bg(p, W, H, accent=None, right="", national=False, ms=0):
    g = QLinearGradient(0, 0, 0, H)
    if national:                                   # blood-red, pulsing: the scariest screen DeerDec has
        pulse = 0.5 + 0.5 * math.sin(ms / 1000.0 * 2 * math.pi * 0.9)
        g.setColorAt(0, QColor(int(70 + 60 * pulse), 0, 0))
        g.setColorAt(1, QColor(int(16 + 14 * pulse), 0, 0))
        accent = (255, 255, 255) if (ms // 400) % 2 == 0 else (255, 30, 30)
    else:
        g.setColorAt(0, QColor(*DEER["bg_top"]))
        g.setColorAt(1, QColor(*DEER["bg_bot"]))
    p.fillRect(QRectF(0, 0, W, H), g)
    fam = ["Arial", "Liberation Sans", "Helvetica"]
    p.setFont(mkfont(fam, H * 0.05, True, spacing=130))
    p.setPen(QColor(*DEER["amber"]))
    p.drawText(QRectF(W * 0.04, H * 0.025, W * 0.5, H * 0.075), Qt.AlignLeft | Qt.AlignVCenter,
               str(_style.get("corner") or "DEERDEC").upper())
    if right:
        p.setFont(mkfont(fam, H * 0.038, True))
        p.setPen(QColor(*DEER["dim"]))
        p.drawText(QRectF(W * 0.46, H * 0.025, W * 0.5, H * 0.075), Qt.AlignRight | Qt.AlignVCenter, right)
    p.fillRect(QRectF(0, H * 0.105, W, H * (0.022 if national else 0.011)), QColor(*(accent or DEER["amber"])))


def _deer_lines(text, px, W, fam=("Arial", "Liberation Sans", "Helvetica")):
    return wrap_px(text, QFontMetrics(mkfont(list(fam), px, True)), W)


def _standby_deerdec(p, W, H, texts):
    _deer_bg(p, W, H)
    p.setFont(mkfont(["Arial", "Liberation Sans", "Helvetica"], H * 0.085, True, spacing=115))
    p.setPen(QColor(*DEER["fg"]))
    text_c(p, "DETAILS CHANNEL", H * 0.40, H * 0.12, W)


STANDBY["deerdec"] = _standby_deerdec


# ---- the alert-area map on the DeerDec slide: the polygon if there is one, else the affected counties / state,
# else (whole US) the mainland. County shapes come from data/counties.json (the Map tab downloads it).
MAP_SHAPES = None        # set by the app: rec -> polygon rings [[lat, lon], ...] borrowed from NWS / CAP
_COUNTIES = {"data": None, "loading": False, "failed": False}
CONUS = (-125.0, 24.0, -66.5, 49.6)          # lon0, lat0, lon1, lat1


# Island outlines (rough, hand-simplified) for the places the county file does not cover: key = SAME state+county.
_TERR_RAW = {
    "66010": [[(144.63, 13.30), (144.64, 13.43), (144.70, 13.52), (144.80, 13.60), (144.86, 13.65), (144.95, 13.59),
               (144.89, 13.50), (144.85, 13.43), (144.76, 13.35), (144.73, 13.24), (144.66, 13.24)]],            # Guam
    "69100": [[(145.69, 15.12), (145.70, 15.24), (145.76, 15.28), (145.82, 15.20), (145.78, 15.12), (145.72, 15.10)]],  # Saipan
    "69110": [[(145.60, 14.99), (145.62, 15.08), (145.66, 15.09), (145.65, 14.95), (145.61, 14.95)]],                 # Tinian
    "69085": [[(145.12, 14.13), (145.14, 14.17), (145.22, 14.18), (145.27, 14.15), (145.18, 14.11)]],                 # Rota
    "69120": [[(145.72, 18.14), (145.78, 18.17), (145.80, 18.09), (145.75, 18.05)],                                   # Northern Islands
              [(145.67, 16.35), (145.70, 16.37), (145.71, 16.33)], [(145.84, 16.69), (145.87, 16.71), (145.88, 16.67)]],
    "60010": [[(-170.84, -14.35), (-170.76, -14.27), (-170.65, -14.26), (-170.56, -14.29), (-170.62, -14.33),
               (-170.72, -14.37), (-170.80, -14.38)]],                                                               # Tutuila (East)
    "60020": [[(-169.53, -14.26), (-169.45, -14.25), (-169.46, -14.21), (-169.52, -14.22)],                          # Manu'a
              [(-169.70, -14.18), (-169.60, -14.17), (-169.62, -14.19)]],
    "78010": [[(-64.90, 17.72), (-64.85, 17.78), (-64.70, 17.80), (-64.57, 17.76), (-64.60, 17.70), (-64.80, 17.69)]],  # St. Croix
    "78020": [[(-64.79, 18.34), (-64.74, 18.38), (-64.68, 18.36), (-64.70, 18.32), (-64.76, 18.31)]],                  # St. John
    "78030": [[(-65.09, 18.34), (-65.00, 18.37), (-64.90, 18.36), (-64.85, 18.33), (-64.95, 18.31), (-65.05, 18.31)]],  # St. Thomas
    "72000": [[(-67.27, 18.36), (-67.15, 18.50), (-66.5, 18.49), (-65.8, 18.45), (-65.22, 18.38), (-65.30, 18.20),
               (-65.7, 18.0), (-66.2, 17.93), (-66.9, 17.95), (-67.2, 18.05)]],                                      # Puerto Rico (only if the file lacks it)
}


def _blob(cx, cy, r, n=9):
    """A small rounded outline standing in for an island or atoll."""
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


# Pacific / minor outlying islands: shown as small marks at their real positions (too small to outline here).
_ISLANDS = {
    "68000": [(171.38, 7.09, .2), (171.62, 7.05, .15), (167.73, 8.85, .25), (162.35, 11.35, .2), (165.5, 11.6, .2),
              (166.9, 11.2, .15), (169.85, 11.23, .12), (170.0, 9.45, .15), (169.6, 5.9, .15), (171.9, 6.1, .15),
              (168.7, 7.3, .2), (169.3, 9.9, .15)],                                         # Marshall Islands
    "64000": [(151.78, 7.42, .35), (158.22, 6.88, .16), (162.98, 5.32, .12), (138.12, 9.55, .14), (139.7, 10.0, .12),
              (143.9, 7.35, .12), (154.95, 3.85, .1), (154.78, 1.1, .1)],                    # Micronesia (Chuuk, Pohnpei, Kosrae, Yap...)
    "74000": [(-177.38, 28.2, .15), (166.65, 19.29, .12), (-169.52, 16.73, .1), (-176.48, 0.19, .08),
              (-176.62, 0.81, .08), (-160.02, -0.37, .08), (-162.4, 6.38, .08), (-162.08, 5.88, .08)],  # US Minor Outlying Is.
}
_TERR_RAW.update({k: [_blob(*v) for v in vs] for k, vs in _ISLANDS.items()})
_TERR_RAW["70000"] = [[(134.55, 7.38), (134.65, 7.40), (134.67, 7.70), (134.60, 7.73), (134.52, 7.56)],      # Palau: Babeldaob
                      _blob(134.45, 7.30, .07), _blob(134.25, 7.0, .08), _blob(134.15, 6.9, .05),            # Koror, Peleliu, Angaur
                      _blob(134.70, 8.08, .06), _blob(132.2, 5.33, .06), _blob(131.12, 3.0, .05)]            # Kayangel, Sonsorol, Tobi


def _terr_features(have):
    out = {}
    for k, rings in _TERR_RAW.items():
        if k == "72000" and any(h.startswith("72") for h in have):
            continue
        if k in have:
            continue
        pts = [q for r in rings for q in r]
        out[k] = {"bbox": (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts)),
                  "rings": [list(r) for r in rings]}
    return out


def _load_counties():
    try:
        path = os.path.join("data", "counties.json")
        if not os.path.exists(path):
            _COUNTIES["failed"] = True
            _COUNTIES["data"] = _terr_features({})           # no county file yet: the islands still work
            return
        with open(path, "r", encoding="utf-8") as fh:
            gj = json.load(fh)
        out = {}
        for ft in gj.get("features", []):
            g = ft.get("geometry") or {}
            polys = g.get("coordinates") or []
            if g.get("type") == "Polygon":
                polys = [polys]
            rings = [pl[0] for pl in polys if pl]
            pts = [pt for r in rings for pt in r]
            if not pts:
                continue
            xs, ys = [q[0] for q in pts], [q[1] for q in pts]
            fid = str(ft.get("id") or ft.get("properties", {}).get("GEO_ID", ""))[-5:].zfill(5)
            if fid.startswith("02"):                         # Alaska: the Aleutians cross the date line - keep one piece
                rings = [[(x - 360 if x > 0 else x, y) for x, y in r] for r in rings]
                xs = [x - 360 if x > 0 else x for x in xs]
            out[fid] = {"bbox": (min(xs), min(ys), max(xs), max(ys)), "rings": rings}
        out.update(_terr_features(set(out)))                 # Guam, Marianas, American Samoa, Virgin Islands, ...
        _COUNTIES["data"] = out
    except Exception:
        _COUNTIES["failed"] = True
    finally:
        _COUNTIES["loading"] = False


def _counties():
    if _COUNTIES["data"] is None and not _COUNTIES["loading"] and not _COUNTIES["failed"]:
        _COUNTIES["loading"] = True
        threading.Thread(target=_load_counties, daemon=True).start()
    return _COUNTIES["data"]


def _deer_area(rec):
    """(polygon rings as [lon, lat], affected county ids, whole_us) for the alert."""
    al = getattr(rec, "alert", None)
    polys = list(getattr(al, "polygons", None) or [])
    if not polys and MAP_SHAPES:
        try:
            polys = list(MAP_SHAPES(rec) or [])
        except Exception:
            polys = []
    rings = [[(pt[1] - 360 if (pt[1] > 170 and pt[0] > 50) else pt[1], pt[0]) for pt in r] for r in polys if r]
    fips = [str(c).zfill(6) for c in (getattr(al, "fips", None) or [])]
    whole = (not fips and getattr(rec, "is_national", False)) or any(c[1:] == "00000" for c in fips)
    return rings, fips, whole


def _deer_map_pixmap(rec, w, h, col):
    rings, fips, whole = _deer_area(rec)
    data = _counties()
    ids = set()
    if data and not rings and not whole:
        for c in fips:
            if c[3:] == "000":                                       # a whole state
                ids.update(k for k in data if k.startswith(c[1:3]))
            elif c[1:] in data:
                ids.add(c[1:])
            elif c[1:3] in ("60", "64", "66", "68", "69", "70", "72", "74", "78"):     # a territory district we only have as islands: show them all
                ids.update(k for k in data if k.startswith(c[1:3]))
    wrap = False                                    # places on both sides of the date line (Pacific): put them side by side
    if whole:
        view = CONUS
    elif rings:
        pts = [q for r in rings for q in r]
        view = (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))
        wrap = view[2] - view[0] > 180
    elif ids:
        bb = [data[i]["bbox"] for i in ids]
        view = (min(b[0] for b in bb), min(b[1] for b in bb), max(b[2] for b in bb), max(b[3] for b in bb))
        wrap = view[2] - view[0] > 180
    else:
        return None
    if wrap:
        def lx(x):
            return x + 360 if x < 0 else x
        if rings:
            pts = [(lx(q[0]), q[1]) for r in rings for q in r]
        else:
            pts = [(lx(b[i]), b[j]) for i in (0, 2) for j in (1, 3) for b in (data[k]["bbox"] for k in ids)]
        view = (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))
    else:
        def lx(x):
            return x
    x0, y0, x1, y1 = view
    mid = math.radians((y0 + y1) / 2)
    kx = max(0.2, math.cos(mid))
    spx, spy = max((x1 - x0) * kx, 0.9 * kx), max(y1 - y0, 0.9)         # never zoom in closer than ~1 degree
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    sc = min(w / (spx * 1.3), h / (spy * 1.3))

    def pt(q):
        return QPointF(w / 2 + (lx(q[0]) - cx) * kx * sc, h / 2 - (q[1] - cy) * sc)

    pm = QPixmap(w, h)
    pm.fill(QColor(8, 22, 20))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    vx0, vx1 = cx - w / 2 / (kx * sc), cx + w / 2 / (kx * sc)
    vy0, vy1 = cy - h / 2 / sc, cy + h / 2 / sc
    if data:
        hl = QColor(*col)
        hl.setAlpha(120)
        for k, ft in data.items():
            b = ft["bbox"]
            if wrap and b[2] < 0:
                b = (b[0] + 360, b[1], b[2] + 360, b[3])
            if b[2] < vx0 or b[0] > vx1 or b[3] < vy0 or b[1] > vy1:
                continue
            mark = whole or k in ids
            p.setPen(QPen(QColor(*col) if mark and not whole else QColor(58, 94, 78), 2 if mark and not whole else 1))
            p.setBrush(hl if mark else QColor(20, 46, 38))
            for r in ft["rings"]:
                p.drawPolygon(QPolygonF([pt(q) for q in r]))
    if rings:
        fill = QColor(*col)
        fill.setAlpha(120)
        p.setPen(QPen(QColor(*col), 3))
        p.setBrush(fill)
        for r in rings:
            p.drawPolygon(QPolygonF([pt(q) for q in r]))
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(90, 130, 110), 2))
    p.drawRect(1, 1, w - 2, h - 2)
    p.end()
    return pm


EXTRA_TEXT = None        # set by the app: rec -> message text borrowed from the matching NWS / CAP alert ("" if none)


class DeerDecScreen(PaintScreen):
    def _message(self):
        """The alert's own message text, else whatever NWS / CAP text has been found for it (it can arrive late)."""
        r = self.rec
        msg = (getattr(r, "message", "") or "").strip()
        if not msg and EXTRA_TEXT:
            try:
                msg = (EXTRA_TEXT(r) or "").strip()
            except Exception:
                msg = ""
        return " ".join(msg.split())

    def _tick(self):
        sig = self._message() if self.rec else ""
        if sig != getattr(self, "_msg_sig", None):
            self._msg_sig, self._key = sig, None               # new text found: rebuild the slides
        if self.rec:
            rings, fips, whole = _deer_area(self.rec)
            ssig = (sum(len(r) for r in rings), tuple(fips), whole, _COUNTIES["data"] is not None)
            if ssig != getattr(self, "_shape_sig", None):
                self._shape_sig, self._map = ssig, None        # polygon / county data arrived: redraw the map
        super()._tick()

    def build_pages(self, W, H):
        pages = [("main", None)]
        msg = self._message()
        if msg:
            px = H * 0.05
            lines = _deer_lines(msg, px, W * 0.88)
            per = max(2, int(H * 0.40 / (px * 1.35)))
            ch = chunks(lines, per)[:DEER["max_msg_pages"]]
            for i, c in enumerate(ch):
                if i == len(ch) - 1 and len(lines) > per * len(ch) and c:
                    c = c[:-1] + [c[-1].rstrip(" .,") + " ..."]
                pages.append(("msg", (c, i + 1, len(ch))))
        return pages

    def _areas(self, p, W, H):
        r = self.rec
        fam = ["Arial", "Liberation Sans", "Helvetica"]
        p.setFont(mkfont(fam, H * 0.032, True, spacing=120))
        p.setPen(QColor(*DEER["amber"]))
        p.drawText(QRectF(W * 0.06, H * 0.695, W * 0.88, H * 0.05), Qt.AlignLeft | Qt.AlignVCenter, "AREAS AFFECTED")
        px = H * 0.042
        lines = _deer_lines("  \u2022  ".join(a.upper() for a in r.areas) or "AREA NOT SPECIFIED", px, W * 0.88)
        if len(lines) > DEER["max_area_lines"]:
            lines = lines[:DEER["max_area_lines"] - 1] + ["... AND MORE"]
        p.setFont(mkfont(fam, px, True))
        p.setPen(QColor(*DEER["fg"]))
        y = H * 0.75
        for ln in lines:
            p.drawText(QRectF(W * 0.06, y, W * 0.88, px * 1.3), Qt.AlignLeft | Qt.AlignVCenter, ln)
            y += px * 1.3

    def draw(self, p, W, H, page, ms):
        r = self.rec
        kind = _bulletin_kind(r)
        col = BULLETIN["event"].get(kind, BULLETIN["event"]["alert"])
        until = f"UNTIL {r.end_local.upper()}" if r.end_local else ""
        nat = kind == "national"
        clear = kind == "clear"                                    # EAT: the national emergency is over
        _deer_bg(p, W, H, col, "NATIONAL ALERT" if nat else "ALL CLEAR" if clear else until, nat, ms)
        fam = ["Arial", "Liberation Sans", "Helvetica"]
        if nat:
            col = (255, 255, 255)                                  # white on blood red reads loudest
        mode, data = page
        if mode == "main":
            lw = W * 0.46                                          # text column left, map right
            px, top, bottom = H * 0.10, H * 0.16, H * 0.50
            while True:
                lines = _deer_lines(r.title.upper(), px, lw)
                lh = px * 1.2
                if (len(lines) <= 4 and len(lines) * lh <= bottom - top) or px <= H * 0.04:
                    break
                px *= 0.93
            p.setFont(mkfont(fam, px, True))
            y = top + ((bottom - top) - len(lines) * lh) / 2
            for ln in lines:
                p.setPen(QColor("#400000" if nat else "#03100c"))
                d = max(2, int(H * (0.009 if nat else 0.005)))
                p.drawText(QRectF(W * 0.04 + d, y + d, lw, lh), Qt.AlignLeft | Qt.AlignVCenter, ln)
                p.setPen(QColor(*col))
                p.drawText(QRectF(W * 0.04, y, lw, lh), Qt.AlignLeft | Qt.AlignVCenter, ln)
                y += lh
            if r.end_local or nat or clear:
                t = ("THE NATIONAL EMERGENCY HAS ENDED" if clear else
                     "UNTIL FURTHER NOTICE" if nat else f"IN EFFECT UNTIL {r.end_local.upper()}")
                tpx = H * 0.045
                tf = QFontMetrics(mkfont(fam, tpx, True, spacing=105))
                tpx *= min(1.0, lw / max(1, tf.horizontalAdvance(t)))
                p.setFont(mkfont(fam, tpx, True, spacing=105))
                p.setPen(QColor(*DEER["fg"]))
                p.drawText(QRectF(W * 0.04, H * 0.53, lw, H * 0.07), Qt.AlignLeft | Qt.AlignVCenter, t)
            mx, my, mw, mh = int(W * 0.53), int(H * 0.15), int(W * 0.43), int(H * 0.46)
            key = (mw, mh, getattr(self, "_shape_sig", None), tuple(col))
            if getattr(self, "_map_key", None) != key or getattr(self, "_map", None) is None:
                self._map_key = key
                self._map = _deer_map_pixmap(r, mw, mh, col)
            if self._map is not None:
                p.drawPixmap(mx, my, self._map)
            else:
                p.setPen(QPen(QColor(90, 130, 110), 2))
                p.setBrush(QColor(8, 22, 20))
                p.drawRect(mx, my, mw, mh)
                p.setFont(mkfont(fam, H * 0.035, True, spacing=115))
                p.setPen(QColor(*DEER["dim"]))
                loading = _COUNTIES["loading"] or (_COUNTIES["data"] is None and not _COUNTIES["failed"])
                p.drawText(QRectF(mx, my, mw, mh), Qt.AlignCenter,
                           "LOADING MAP..." if loading else "MAP UNAVAILABLE")
            if nat and (ms // 500) % 2 == 0:                       # flashing warning band
                band = QRectF(0, H * 0.625, W, H * 0.06)
                p.fillRect(band, QColor(200, 0, 0))
                p.setFont(mkfont(fam, H * 0.042, True, spacing=125))
                p.setPen(QColor("white"))
                p.drawText(band, Qt.AlignCenter, "SEEK SHELTER NOW")
        else:
            chunk, i, n = data
            p.setFont(mkfont(fam, H * 0.05, True))
            p.setPen(QColor(*col))
            p.drawText(QRectF(W * 0.06, H * 0.13, W * 0.88, H * 0.075), Qt.AlignLeft | Qt.AlignVCenter, r.title.upper())
            p.setFont(mkfont(fam, H * 0.03, True, spacing=120))
            p.setPen(QColor(*DEER["dim"]))
            p.drawText(QRectF(W * 0.06, H * 0.205, W * 0.88, H * 0.05), Qt.AlignLeft | Qt.AlignVCenter,
                       f"MESSAGE  {i} / {n}")
            px = H * 0.05
            p.setFont(mkfont(fam, px, True))
            p.setPen(QColor(*DEER["fg"]))
            y = H * 0.27
            for ln in chunk:
                p.drawText(QRectF(W * 0.06, y, W * 0.88, px * 1.3), Qt.AlignLeft | Qt.AlignVCenter, ln)
                y += px * 1.35
        self._areas(p, W, H)


# ------------------------------------------------------------------ registry
# key -> (menu label, factory(page_seconds)).  New CGEN look = add a line here.
STYLES = {
    "easyplus": ("EASyPLUS", lambda ps: EasyPlusScreen()),
    "dasdec": ("DASDEC-II", lambda ps: DasdecScreen(ps)),
    "easycap": ("EASyCAP", lambda ps: EasyCapScreen(ps)),
    "vds": ("VDS", lambda ps: VdsScreen(ps)),
    "cems1000": ("CEMS-1000", lambda ps: CemsScreen(ps)),
    "xfinity": ("Xfinity", lambda ps: XfinityScreen(ps)),
    "psc902": ("PSC-902", lambda ps: Psc902Screen(ps)),
    "atari800": ("Atari 800XL", lambda ps: Atari800Screen(ps)),
    "deerdec": ("DeerDec", lambda ps: DeerDecScreen(ps)),
    "bulletin": ("Weather Bulletin (fictional)", lambda ps: BulletinScreen(ps)),
    "crawl": ("Crawl (color bands)", lambda ps: CrawlScreen()),
}
ALIASES = {"classic": "easyplus", "digital": "dasdec"}


def make_screen(style="dasdec", page_seconds=8.0):
    style = ALIASES.get(style, style)
    return STYLES.get(style, STYLES["dasdec"])[1](page_seconds)
