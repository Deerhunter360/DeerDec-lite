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
from pathlib import Path

from PySide6.QtCore import QSize, QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QImage, QPixmap, QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication,
                           QLinearGradient, QPainter)
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

ORIGINATOR_NAMES = {
    "PEP": "Primary Entry Point System",
    "CIV": "Civil Authorities",
    "WXR": "National Weather Service",
    "EAS": "EAS Participant",
}

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
_style = {"family": "EAS OSD Mono", "classic_scale": 1.0, "custom": None,
          "digital_font": None, "easycap": None, "atari": None, "seconds": 60, "screen": -1}


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
    """Full-screen on the chosen monitor (e.g. the projector)."""
    screens = QGuiApplication.screens()
    i = _style["screen"]
    if 0 <= i < len(screens):
        widget.setScreen(screens[i])
        widget.move(screens[i].geometry().topLeft())
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
    "crawl": ("Crawl (color bands)", lambda ps: CrawlScreen()),
}
ALIASES = {"classic": "easyplus", "digital": "dasdec"}


def make_screen(style="dasdec", page_seconds=8.0):
    style = ALIASES.get(style, style)
    return STYLES.get(style, STYLES["dasdec"])[1](page_seconds)
