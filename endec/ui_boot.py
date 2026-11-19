"""DeerDec startup sequence: shows what is starting (config, tables, fonts, IPAWS, audio streams)."""
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

COLORS = {"ok": "#39ff7a", "warn": "#ffd400", "fail": "#ff4a3d"}
TAGS = {"ok": " OK ", "warn": "WARN", "fail": "FAIL"}


class Step:
    """run() -> (state, msg) when finished at once, or None if it needs time;
    then check() is polled (every 250 ms) until it returns (state, msg) or timeout seconds pass."""

    def __init__(self, label, run, check=None, timeout=10.0, timeout_msg="no response yet"):
        self.label, self.run, self.check = label, run, check
        self.timeout, self.timeout_msg = timeout, timeout_msg


class BootScreen(QWidget):
    finished = Signal()

    def __init__(self, steps):
        super().__init__()
        self.steps, self.i, self.lines, self.skip, self.t0 = steps, 0, [], False, 0.0
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("background:#04100a;")
        self.resize(820, 520)
        g = QGuiApplication.primaryScreen().geometry()
        self.move(g.center().x() - 410, g.center().y() - 260)
        v = QVBoxLayout(self)
        v.setContentsMargins(34, 26, 34, 22)
        title = QLabel("DEERDEC")
        tf = QFont("Consolas", 40)
        tf.setBold(True)
        tf.setLetterSpacing(QFont.AbsoluteSpacing, 8)
        title.setFont(tf)
        title.setStyleSheet("color:#39ff7a;")
        sub = QLabel("EMERGENCY ALERT SYSTEM  ENCODER / DECODER")
        sub.setFont(QFont("Consolas", 11))
        sub.setStyleSheet("color:#1f9d4d;")
        self.body = QLabel("")
        self.body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.body.setTextFormat(Qt.RichText)
        self.body.setFont(QFont("Consolas", 12))
        self.body.setStyleSheet("color:#39ff7a;")
        hint = QLabel("click or press Esc to skip")
        hint.setFont(QFont("Consolas", 9))
        hint.setStyleSheet("color:#14602f;")
        v.addWidget(title)
        v.addWidget(sub)
        v.addSpacing(14)
        v.addWidget(self.body, 1)
        v.addWidget(hint, alignment=Qt.AlignRight)
        self.poll = QTimer(self)
        self.poll.timeout.connect(self._poll)

    def start(self):
        self.show()
        QTimer.singleShot(400, self._next)

    # ---- sequencing
    def _render(self):
        self.body.setText("<br>".join(self.lines))

    def _line(self, label, state=None, msg=""):
        if state is None:
            return f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{label} …"
        c = COLORS[state]
        return (f"[<span style='color:{c}'>{TAGS[state]}</span>] {label}"
                f" <span style='color:#1f9d4d'>— {msg}</span>")

    def _next(self):
        if self.i >= len(self.steps):
            self.lines.append("<br><span style='color:#ffffff'>DEERDEC ONLINE</span>")
            self._render()
            QTimer.singleShot(1300, self.finished.emit)
            return
        st = self.steps[self.i]
        try:
            res = st.run()
        except Exception as e:
            res = ("fail", str(e)[:80])
        if res is None and not self.skip:
            self.lines.append(self._line(st.label))
            self._render()
            self.t0 = time.time()
            self.poll.start(250)
            return
        self._done(st, res or ("warn", "skipped"), append=True)

    def _poll(self):
        st = self.steps[self.i]
        res = st.check() if st.check else None
        if res is None and time.time() - self.t0 > st.timeout:
            res = ("warn", st.timeout_msg)
        if res is not None:
            self.poll.stop()
            self._done(st, res, append=False)

    def _done(self, st, res, append):
        line = self._line(st.label, *res)
        if append:
            self.lines.append(line)
        else:
            self.lines[-1] = line
        self._render()
        self.i += 1
        QTimer.singleShot(0 if self.skip else 260, self._next)

    def _skip(self):
        self.skip = True
        self.poll.stop()
        if self.i < len(self.steps) and self.lines and "…" in self.lines[-1]:
            self.lines[-1] = self._line(self.steps[self.i].label, "warn", "continuing in background")
            self.i += 1
        QTimer.singleShot(0, self._next)

    def mousePressEvent(self, _):
        self._skip()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Space):
            self._skip()
