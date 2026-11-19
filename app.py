#!/usr/bin/env python3
"""DeerDec - EAS encoder / decoder / CGEN.   Run:  python app.py   (pip install -r requirements.txt)"""
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QTabWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from endec import Alert, Config, Endec, defs
from endec import cgen
from endec.config import CONFIG_FILE
from endec.player import play_wav, stop_audio
from endec import audio_out
from endec.mapserver import MapServer
from endec.ui_map import MapTab
from endec.same import SameHeader
from endec.ui_boot import BootScreen, Step
from endec.ui_tabs import SourcesTab

ORG_ITEMS = [("EAS", "Broadcast station / cable system"), ("CIV", "Civil authorities"),
             ("WXR", "National Weather Service"), ("PEP", "Primary Entry Point")]
STATE_COLORS = {"OK": "#2ecc71", "Polling…": "#f1c40f", "Starting…": "#f1c40f",
                "ERROR": "#e74c3c", "Disabled": "#95a5a6", "Stopped": "#95a5a6", "Not started": "#95a5a6"}
AUDIO_OK = ("Listening",)


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.setWindowTitle("DeerDec Lite")
        self.setMinimumSize(640, 480)
        self._restore_geometry(cfg)
        self.cfg = cfg
        self.engine = Endec(cfg)
        self.records, self.current_rec = [], None
        self.pending, self.busy_until = [], 0.0       # alerts waiting their turn / when the current one ends
        self.screen_win = self._make_screen()
        self.listener = None
        if self.engine.monitor:
            self.engine.monitor.busy_cb = self._hold_live
        if self.engine.monitor:
            try:
                from endec.listen import AudioListener
                self.listener = AudioListener(self.engine.monitor, self)
            except Exception:
                self.listener = None

        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._dispatch)
        self.queue_timer.start(700)
        self.watchdog_timer = QTimer(self)            # revive pollers / monitors that died
        self.watchdog_timer.timeout.connect(self.engine.supervise)
        self.watchdog_timer.start(5000)

        tabs = QTabWidget()
        tabs.addTab(self._monitor_tab(), "Monitor")
        QShortcut(QKeySequence("Ctrl+Shift+A"), self, activated=self._abort_alert)
        self.map_server, self.map_url = MapServer(self.engine, cfg.map_port), ""
        try:
            self.map_url = self.map_server.start()
        except Exception as ex:
            self.engine.log(f"Map server failed: {ex}")
        tabs.addTab(MapTab(lambda: self.map_url + "map" if self.map_url else ""), "Map")
        self.sources_tab = SourcesTab(cfg, self.engine, on_volume=self._volume)
        tabs.addTab(self.sources_tab, "Sources")
        tabs.addTab(self._config_tab(), "Config")
        self.setCentralWidget(tabs)

        sb = self.statusBar()
        self.sb_label = QLabel("")
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        self.sb_label.setFont(f)
        sb.addWidget(self.sb_label, 1)
        sb.setMinimumHeight(34)
        sb.setStyleSheet("QStatusBar{background:#1c1f26;} QLabel{color:#e8e8e8; padding:4px 8px;}")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(200)
        self.stimer = QTimer(self)
        self.stimer.timeout.connect(self._refresh_status)
        self.stimer.start(500)
        self._refresh_status()

    # ================================================================ startup sequence
    def boot_steps(self):
        cfg, e = self.cfg, self.engine
        auto = cfg.auto_start
        audio_srcs = [s for s in cfg.sources if s["kind"] in ("stream", "device") and s.get("enabled", True)]

        def fonts():
            got = [n for n, k in (("EAS OSD Mono", "custom"), ("Luxi Mono", "digital_font"),
                                  ("EASyCAP pixel", "easycap"), ("Atari pixel", "atari")) if cgen._style.get(k)]
            return ("ok", ", ".join(got)) if got else ("warn", "no custom fonts found in fonts/")

        def displays():
            n = len(QGuiApplication.screens())
            i = cfg.cgen_screen
            return ("ok", f"{n} display(s); CGEN on "
                          f"{'#' + str(i + 1) if 0 <= i < n else 'primary'}")


        def ipaws_run():
            if not auto:
                return ("warn", "auto-start is off")
            e.start_ipaws()
            return None

        def ipaws_check():
            ip = e.ipaws
            if ip["state"] == "OK":
                return ("ok", f"polling every {cfg.poll_seconds}s, {ip['alerts']} alerts in feed")
            if ip["state"].startswith("ERROR"):
                return ("fail", ip["error"] or "request failed")
            if ip["state"] == "Disabled":
                return ("warn", "IPAWS source is disabled")
            return None

        def audio_run():
            if not auto:
                return ("warn", "auto-start is off")
            if not audio_srcs:
                return ("warn", "no audio sources enabled")
            if not e.monitor:
                return ("fail", "numpy missing (pip install numpy sounddevice)")
            e.start_audio()
            return ("ok", f"{len(audio_srcs)} source(s)")

        steps = [
            Step("Loading configuration", lambda: ("ok", CONFIG_FILE)),
            Step("Loading event code tables",
                 lambda: ("ok", f"{len(defs.SAME__EEE)} events, {len(defs.US_SAME_CODE)} areas")),
            Step("Loading fonts", fonts),
            Step("Detecting displays", displays),
            Step("Starting IPAWS polling", ipaws_run, ipaws_check, 12, "no reply from FEMA yet"),
            Step("Starting audio monitors", audio_run),
        ]
        for s in audio_srcs:
            def chk(name=s["name"]):
                st = ((e.monitor.status.get(name) if e.monitor else None) or {}).get("state", "")
                if st in AUDIO_OK:
                    return ("ok", "listening, decoding silently")
                if st.startswith(("ffmpeg not found", "Error", "Device not found", "pip install")):
                    return ("fail", st)
                return None
            steps.append(Step(f"Connecting {s['name']}",
                              (lambda: ("warn", "not started")) if not (auto and e.monitor) else (lambda: None),
                              chk, 10, "still connecting"))
        return steps

    # ================================================================ screen / style
    def _make_screen(self):
        sc = cgen.make_screen(self.cfg.cgen_style, self.cfg.cgen_page_seconds)
        sc.switch_cb = self._switch_from_key
        return sc

    def _rebuild_screen(self):
        was_up = self.screen_win.isVisible()
        self.screen_win.hide()
        self.screen_win = self._make_screen()
        if was_up and self.current_rec:
            self._show(self.current_rec)

    def _style_changed(self):
        self.cfg.cgen_style = self.style_cb.currentData()
        self.cfg.save()
        self._rebuild_screen()

    def _screen_changed(self):
        self.cfg.cgen_screen = self.screen_cb.currentData()
        self.cfg.save()
        cgen.configure(self.cfg)
        if self.screen_win.isVisible() and self.current_rec:
            self._show(self.current_rec)

    def _switch_from_key(self, style):
        self.style_cb.setCurrentIndex(max(0, self.style_cb.findData(style)))

    def _show(self, rec, dur=None):
        self.current_rec = rec
        self.screen_win.show_record(rec, dur or max(self.cfg.cgen_seconds, int(rec.seconds) + 2))



    def _abort_alert(self):
        """Emergency stop: silence audio, drop the screen, empty the queue, discard any recording/live relay."""
        try:
            stop_audio()
            self.pending.clear()
            self.busy_until = 0.0
            self.screen_win.close_timer.stop()
            self.screen_win.hide()
            if self.engine.monitor:
                self.engine.monitor.abort()
            self.engine._audio_recs.clear()
            self._log("ABORT: alert stopped, queue cleared")
        except Exception as e:
            self._log(f"Abort problem: {e}")

    # ---- alert queue: one alert at a time, others wait their turn
    def _busy(self):
        return time.time() < self.busy_until

    def _hold_live(self):
        """True when a newly heard alert must NOT be relayed live: something is already on air, queued,
        being built, or being relayed live.  It is recorded instead and plays, tones and all, in its turn."""
        m = self.engine.monitor
        return bool(self._busy() or self.pending or self.engine.building
                    or (m is not None and m.relay_source is not None))

    def _dispatch(self):
        if self._busy():
            return
        for i, rec in enumerate(self.pending):
            if rec.ready:
                self.pending.pop(i)
                self._present(rec)
                return

    def _present(self, rec):
        try:
            if rec.from_audio and rec.mode == "live":
                dur = self.cfg.relay_max_seconds + 10       # live audio is already playing; trimmed at the EOM
            else:
                dur = max(self.cfg.cgen_seconds, int(rec.seconds) + 2)
                if rec.wav and (rec.from_audio or self.cfg.auto_play) and rec.mode != "live":
                    self._log(f"Playing {'recording' if rec.from_audio else 'alert audio'} ({rec.seconds:.0f}s)")
                    play_wav(rec.wav)
            self.busy_until = time.time() + dur
            if self.cfg.auto_cgen:
                self._show(rec, dur)
        except Exception as e:
            self.busy_until = 0.0
            self._log(f"Could not present alert: {e}")

    def _volume(self, v):
        if self.listener:
            self.listener.set_volume(v)

    # ================================================================ Monitor tab
    def _monitor_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addWidget(QLabel("Style:"))
        self.style_cb = QComboBox()
        for key, (label, _f) in cgen.STYLES.items():
            self.style_cb.addItem(label, key)
        cur = cgen.ALIASES.get(self.cfg.cgen_style, self.cfg.cgen_style)
        self.style_cb.setCurrentIndex(max(0, self.style_cb.findData(cur)))
        row.addWidget(self.style_cb)
        row.addWidget(QLabel("Screen:"))
        self.screen_cb = QComboBox()
        screens = QGuiApplication.screens()
        for i, sc in enumerate(screens):
            g = sc.geometry()
            self.screen_cb.addItem(f"{i + 1}: {sc.name()} ({g.width()}x{g.height()})", i)
        idx = self.cfg.cgen_screen
        if not 0 <= idx < len(screens):
            idx = screens.index(QGuiApplication.primaryScreen())
        self.screen_cb.setCurrentIndex(idx)
        row.addWidget(self.screen_cb)
        self.style_cb.currentIndexChanged.connect(self._style_changed)
        self.screen_cb.currentIndexChanged.connect(self._screen_changed)
        prev = QPushButton("Preview")
        prev.clicked.connect(self._preview)
        row.addWidget(prev)
        row.addStretch()
        ab = QPushButton("ABORT ALERT")
        ab.setStyleSheet("QPushButton{background:#d9363e;color:white;font-weight:700;border:none;"
                         "border-radius:4px;padding:6px 16px;} QPushButton:hover{background:#ff4d55;}")
        ab.setToolTip("Stops the audio, closes the screen, clears the queue and discards a live/recorded alert (Ctrl+Shift+A)")
        ab.clicked.connect(self._abort_alert)
        row.addWidget(ab)
        for text, fn in (("Stop alert audio", stop_audio), ("Refresh connections", self.engine.refresh),
                         ("Replay IPAWS feed", self.engine.replay_feed),
                         ("Stop", self.engine.stop), ("Start", self.engine.start)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)

        box = QGroupBox("Status")
        bl = QVBoxLayout(box)
        self.status_lbl = QLabel("")
        self.status_lbl.setTextFormat(Qt.RichText)
        self.status_lbl.setStyleSheet("font-size:15px; line-height:150%;")
        bl.addWidget(self.status_lbl)
        v.addWidget(box)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "Org", "Code", "Event", "Area"])
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.doubleClicked.connect(self._replay)
        v.addWidget(self.table, 2)
        v.addWidget(QLabel("Double-click an alert to replay audio + screen.   On the alert screen: "
                           "1-9 or C/D/W switch style, Space/→ next page, Esc/click closes."))
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setStyleSheet("background:#111; color:#0f0; font-family:Consolas;")
        v.addWidget(self.logbox, 3)
        return w

    def _refresh_status(self):
        e, ip, now = self.engine, self.engine.ipaws, time.time()
        col = STATE_COLORS.get(ip["state"], "#95a5a6")
        last = (time.strftime("%H:%M:%S", time.localtime(ip["last"])) + f" ({int(now - ip['last'])}s ago)"
                if ip["last"] else "never")
        nxt = f"{max(0, int(ip['next'] - now))}s" if ip["next"] and e.running else "—"
        lines = [f"<span style='color:{col}'>●</span> <b>IPAWS feed:</b> {ip['state']}"
                 f" &nbsp; last poll {last} &nbsp; next in {nxt} &nbsp; "
                 f"{ip['alerts']} alerts in feed, {ip['new']} new &nbsp; ({ip['polls']} polls)"]
        if ip["error"]:
            lines.append(f"&nbsp;&nbsp;&nbsp;<span style='color:#e74c3c'>{ip['error']}</span>")
        n_listen = n_audio = 0
        m = e.monitor
        if m is None:
            lines.append("<span style='color:#95a5a6'>●</span> <b>Audio:</b> unavailable (pip install numpy sounddevice)")
        elif not m.status:
            lines.append("<span style='color:#95a5a6'>●</span> <b>Audio:</b> no sources running")
        for name, st in (m.status.items() if m else []):
            n_audio += 1
            ok = st.get("state") in AUDIO_OK
            n_listen += ok
            c = "#2ecc71" if ok else "#f1c40f" if st.get("state", "").startswith(("Conn", "Start", "Recon")) else "#e74c3c"
            ear = " 🔊" if m.listen_name == name else ""
            last_h = (f" &nbsp; bursts heard: {st.get('bursts', 0)}"
                      + (f" &nbsp; last header: {st['last']}" if st.get("last") else ""))
            lines.append(f"<span style='color:{c}'>●</span> <b>{name}</b>{ear}: {st.get('state', '')}{last_h}")
        watch = "all locations"
        lines.append(f"<span style='color:#95a5a6'>Watching: {watch} &nbsp;|&nbsp; "
                     f"Style: {self.style_cb.currentText()} &nbsp;|&nbsp; Alerts this session: {len(self.records)}</span>")
        self.status_lbl.setText("<br>".join(lines))
        self.sb_label.setText(f"IPAWS: {ip['state']} · next poll {nxt}   |   Audio: {n_listen}/{n_audio} listening   "
                              f"|   Watching {watch}   |   {len(self.records)} alert(s)")

    def _log(self, s):
        self.logbox.appendPlainText(s)

    def _pump(self):
        try:
            while True:
                kind, data = self.engine.events.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "alert":
                    self._on_alert(data)
                elif kind == "relay":
                    self._on_relay(*data)
        except queue.Empty:
            pass

    def _on_alert(self, rec):
        self.records.append(rec)
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, val in enumerate((datetime.now().strftime("%H:%M:%S"), rec.alert.org, rec.alert.eee,
                                 rec.title, "; ".join(rec.areas)[:160])):
            self.table.setItem(r, c, QTableWidgetItem(val))
        self._log(rec.header)
        self._log(rec.text)
        if self._busy() or any(r for r in self.pending):
            self._log(f"Another alert is on air - {rec.title} queued ({len(self.pending) + 1} waiting)")
            if rec.from_audio and rec.mode == "live":
                # This one was set to relay live but has to wait, so there is nothing live to join later.
                # Play its recording (header, attention tone, message, EOM) when its turn comes.
                rec.mode = "record"
                rec.ready = os.path.basename(rec.wav or "").startswith("heard_")      # True once the recording is finished
                self._log(f"{rec.title}: will play from its recording when it is next")
        self.pending.append(rec)
        self._dispatch()


    def _on_relay(self, source, path, secs, rec):
        """Audio following a heard alert has finished recording."""
        self._log(f"Alert audio from {source}: {secs:.0f}s")
        if rec is None:
            return
        if rec.mode == "live":                    # live audio is over: keep the screen up a little longer
            dur = max(self.cfg.cgen_seconds, 10)
            self.busy_until = time.time() + dur
            if self.screen_win.isVisible():
                self.screen_win.close_timer.start(int(dur * 1000))
        self._dispatch()

    def _replay(self):
        r = self.table.currentRow()
        if 0 <= r < len(self.records):
            rec = self.records[r]
            if rec.wav:
                play_wav(rec.wav)
            self._show(rec)

    def _preview(self):
        hdr = SameHeader("CIV", "RMT", ["054039"], "0030", callsign=self.cfg.callsign)
        a = Alert(eee="RMT", org="CIV", fips=hdr.fips, sender_name="Preview",
                  description="This is a preview of the alert screen. No action is required.")
        self._show(self.engine.preview_record(a, hdr))

    # ================================================================ Encoder tab






    # ================================================================ Config tab
    # (key, kind, label, options, hint)   kinds: text / bool / int / float / choice
    CONFIG_SECTIONS = [
        ("General", [
            ("callsign", "text", "Callsign", None, "Up to 8 characters; used in the SAME header and 'Message from ...'."),
            ("auto_start", "bool", "Start IPAWS polling and audio monitors when DeerDec opens", None, ""),
            ("boot_screen", "bool", "Show the startup sequence", None, ""),
                        ("easdata_path", "text", "EASData.json path", None, "Event names and colors."),
        ]),
        ("IPAWS", [
            ("feed_url", "text", "Feed URL", None, ""),
            ("poll_seconds", "int", "Poll every (seconds)", (5, 3600), ""),
            ("only_actual", "bool", "Only 'Actual' alerts (ignore tests and exercises)", None, ""),
            ("skip_backlog", "bool", "On the first poll, skip alerts that were already in the feed", None, ""),
            ("first_poll_recent_minutes", "int", "...but still show alerts from the last (minutes)", (0, 1440),
             "0 = skip everything already in the feed."),
            ("ipaws_play_max_minutes", "int", "Only play IPAWS alerts sent in the last (minutes)", (0, 10080),
             "Older ones are logged (Alert Log) but not played. 0 = no age limit."),
        ]),
        ("Audio", [
            ("level_correct", "bool", "Level correction (even out loud and quiet audio)", None,
             "Brings quiet speech up and loud speech down - TTS, the alert's own audio, recordings and the live relay."),
            ("level_target_db", "int", "Target speech level (dB, closer to 0 = louder)", (-30, -6),
             "-16 is a good start. The tones and headers stay at their own fixed level."),
            ("level_max_gain_db", "int", "Never boost more than (dB)", (0, 30),
             "Stops a near-silent source from turning into loud hiss."),
            ("audio_output", "outdevice", "Speaker / output device", None,
             "Where alerts and the live relay play. Applies when you save."),
            ("attn_seconds", "int", "Attention tone length (seconds)", (3, 30), "For tones DeerDec generates."),
            ("auto_play", "bool", "Play alert audio automatically", None, ""),
            ("use_cap_audio", "bool", "Use the audio supplied with IPAWS alerts", None, "Otherwise a generated voice is used."),
        ]),
        ("Screen", [
            ("auto_cgen", "bool", "Show the alert screen automatically", None, ""),
            ("cgen_seconds", "int", "Minimum time on screen (seconds)", (5, 900), ""),
            ("cgen_page_seconds", "float", "Seconds per page (paged screens)", (2.0, 60.0), ""),
            ("classic_scale", "float", "EASyPLUS text size", (0.5, 2.0), "1.0 = default."),
            ("atari_scale", "float", "Atari 800XL text size", (0.3, 1.6), "1.0 = default."),
            ("cgen_font", "text", "EASyPLUS fallback font", None, "Used only when fonts/eas.ttf is missing."),
        ]),
        ("Map", [
            ("map_show_sources", "bool", "Show alerts heard on streams and inputs on the map", None,
             "Only the SAME header is known for these, so they are shaded by county with no polygon or message text."),
            ("map_port", "int", "Map server port", (1024, 65535),
             "The map is also at http://127.0.0.1:<port>/ for any browser. Change needs a restart."),
        ]),
        ("Voice", [
            ("tts_enabled", "bool", "Generate a voice for alerts that have no audio", None, "Needs pyttsx3."),
            ("tts_include_description", "bool", "Read the alert message too", None, ""),
            ("tts_voice", "text", "Voice (part of its name)", None, "For example: David, Zira. Blank = Windows default."),
            ("tts_rate", "int", "Speaking speed", (80, 300), ""),
        ]),
    ]

    def _config_tab(self):
        from PySide6.QtWidgets import (QDoubleSpinBox, QFrame, QListWidget, QScrollArea, QSpinBox,
                                       QStackedWidget)
        self.fields = {}
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        body = QHBoxLayout()
        nav, stack = QListWidget(), QStackedWidget()
        nav.setFixedWidth(150)
        nav.setStyleSheet("QListWidget{font-size:11pt;} QListWidget::item{padding:8px 10px;}")
        body.addWidget(nav)
        body.addWidget(stack, 1)
        outer.addLayout(body, 1)
        for title, fields in self.CONFIG_SECTIONS:
            page = QWidget()
            pv = QVBoxLayout(page)
            pv.setContentsMargins(18, 10, 18, 10)
            head = QLabel(title)
            head.setStyleSheet("font-size:15pt; font-weight:600;")
            pv.addWidget(head)
            rule = QFrame()
            rule.setFrameShape(QFrame.HLine)
            rule.setStyleSheet("color:#3a3f4b;")
            pv.addWidget(rule)
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
            form.setVerticalSpacing(12)
            form.setHorizontalSpacing(18)
            pv.addLayout(form)
            for key, kind, label, opt, hint in fields:
                val = getattr(self.cfg, key)
                if kind == "bool":
                    w = QCheckBox(label)
                    w.setChecked(bool(val))
                    cell, lab = w, None
                else:
                    if kind == "text":
                        w = QLineEdit(str(val))
                    elif kind == "lines":
                        w = QPlainTextEdit("\n".join(val))
                        w.setFixedHeight(80)
                    elif kind == "outdevice":
                        w = QComboBox()
                        w.addItem(audio_out.DEFAULT_LABEL)
                        w.addItems(audio_out.list_outputs())
                        w.setCurrentText(str(val) if str(val) else audio_out.DEFAULT_LABEL)
                    elif kind == "choice":
                        w = QComboBox()
                        w.addItems(opt)
                        w.setCurrentText(str(val) if str(val) in opt else opt[0])
                    elif kind == "int":
                        w = QSpinBox()
                        w.setRange(*opt)
                        w.setValue(int(val))
                    else:
                        w = QDoubleSpinBox()
                        w.setRange(*opt)
                        w.setSingleStep(0.1)
                        w.setDecimals(2)
                        w.setValue(float(val))
                    w.setMaximumWidth(460 if kind in ("text", "lines") else 220)
                    cell, lab = w, label
                    if kind == "outdevice":
                        w.setMaximumWidth(380)
                        cell = QWidget()
                        row = QHBoxLayout(cell)
                        row.setContentsMargins(0, 0, 0, 0)
                        tb = QPushButton("Test")
                        tb.clicked.connect(self._test_output)
                        row.addWidget(w)
                        row.addWidget(tb)
                        row.addStretch(1)
                self.fields[key] = (kind, w)
                box = QWidget()
                bv = QVBoxLayout(box)
                bv.setContentsMargins(0, 0, 0, 0)
                bv.setSpacing(3)
                bv.addWidget(cell)
                if hint:
                    h = QLabel(hint)
                    h.setWordWrap(True)
                    h.setStyleSheet("color:#8a8f98; font-size:9pt;")
                    bv.addWidget(h)
                if lab is None:
                    form.addRow(box)
                else:
                    form.addRow(QLabel(lab), box)
            pv.addStretch(1)
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QScrollArea.NoFrame)
            sa.setWidget(page)
            stack.addWidget(sa)
            nav.addItem(title)
        nav.currentRowChanged.connect(stack.setCurrentIndex)
        nav.setCurrentRow(0)

        bar = QHBoxLayout()
        self.cfg_status = QLabel("")
        self.cfg_status.setStyleSheet("color:#3dd68c;")
        save = QPushButton("Save changes")
        save.setStyleSheet("QPushButton{background:#3d8bfd;color:white;border:none;border-radius:4px;"
                           "padding:7px 22px;font-weight:600;} QPushButton:hover{background:#5a9eff;}")
        save.clicked.connect(self._save)
        revert = QPushButton("Revert")
        revert.clicked.connect(self._config_revert)
        bar.addWidget(self.cfg_status)
        bar.addStretch(1)
        bar.addWidget(revert)
        bar.addWidget(save)
        outer.addLayout(bar)
        return root

    def _field_get(self, key):
        kind, w = self.fields[key]
        if kind == "bool":
            return w.isChecked()
        if kind == "text":
            return w.text().strip()
        if kind == "lines":
            return [x.strip() for x in w.toPlainText().splitlines() if x.strip()]
        if kind == "outdevice":
            t = w.currentText()
            return "" if t == audio_out.DEFAULT_LABEL else t
        if kind == "choice":
            return w.currentText()
        return w.value()

    def _field_set(self, key, val):
        kind, w = self.fields[key]
        if kind == "bool":
            w.setChecked(bool(val))
        elif kind == "text":
            w.setText(str(val))
        elif kind == "lines":
            w.setPlainText("\n".join(val))
        elif kind in ("outdevice", "choice"):
            w.setCurrentText(str(val) if val else audio_out.DEFAULT_LABEL if kind == "outdevice" else str(val))
        else:
            w.setValue(val)

    def _test_output(self):
        """Play a short tone on whatever is selected in the dropdown (before saving)."""
        import os
        import tempfile
        old = audio_out._choice
        audio_out.set_output(self._field_get("audio_output"))
        path = os.path.join(tempfile.gettempdir(), "deerdec_test.wav")
        with open(path, "wb") as f:
            f.write(audio_out.beep_wav())
        if not audio_out.play_wav_device(path, self):
            self._log("Output test failed for that device")
        audio_out._choice = old

    def _config_revert(self):
        for key, (kind, w) in self.fields.items():
            val = getattr(self.cfg, key)
            if kind == "bool":
                w.setChecked(bool(val))
            elif kind == "text":
                w.setText(str(val))
            elif kind == "lines":
                w.setPlainText("\n".join(val))
            elif kind == "outdevice":
                w.setCurrentText(str(val) if val else audio_out.DEFAULT_LABEL)
            elif kind == "choice":
                w.setCurrentText(str(val))
            else:
                w.setValue(val)
        self.cfg_status.setText("Reverted")

    def _save(self):
        try:
            c = self.cfg
            for key in self.fields:
                setattr(c, key, self._field_get(key))
            c.callsign = c.callsign[:8] or "DEERDEC"
            audio_out.set_output(c.audio_output)
            if self.engine.monitor:
                self.engine.monitor.flush_gen += 1      # reopen the live listener on the new output
            first = next((x for x in c.sources if x["kind"] == "ipaws"), None)
            if first:
                first["target"] = c.feed_url                # the Config "Feed URL" is the main (first) IPAWS feed
            c.save()
            cgen.configure(c)
            self._rebuild_screen()
            self._log("Config saved")
            self.cfg_status.setText("Saved")
            QTimer.singleShot(3000, lambda: self.cfg_status.setText(""))
        except Exception as e:
            QMessageBox.warning(self, "Config", str(e))

    def _restore_geometry(self, cfg):
        """Last size/position if saved (and still on a screen), else 80% of the screen."""
        from PySide6.QtCore import QByteArray
        if cfg.window_geometry:
            try:
                if self.restoreGeometry(QByteArray.fromBase64(cfg.window_geometry.encode())):
                    return
            except Exception:
                pass
        g = QGuiApplication.primaryScreen().availableGeometry()
        w, h = min(1180, int(g.width() * 0.8)), min(820, int(g.height() * 0.85))
        self.resize(w, h)
        self.move(g.left() + (g.width() - w) // 2, g.top() + (g.height() - h) // 2)

    @staticmethod
    def _scrolled(widget):
        """Tall forms scroll instead of forcing the window bigger than the screen."""
        from PySide6.QtWidgets import QScrollArea
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QScrollArea.NoFrame)
        sa.setWidget(widget)
        return sa

    def closeEvent(self, e):
        try:
            self.cfg.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
            self.cfg.save()
        except Exception:
            pass
        self.engine.stop()
        try:
            self.map_server.stop()
        except Exception:
            pass
        e.accept()


def _to_front(win):
    """Open above other windows (Windows can refuse to steal focus, so pin on top briefly)."""
    win.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    win.show()
    win.raise_()
    win.activateWindow()

    def release():
        win.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        win.show()
        win.raise_()
    QTimer.singleShot(1500, release)


def main():
    try:                                            # WebEngine must be imported before QApplication exists
        from PySide6.QtCore import Qt, QCoreApplication
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        from PySide6 import QtWebEngineWidgets  # noqa: F401
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    cfg = Config.load()
    cgen.configure(cfg)
    audio_out.set_output(cfg.audio_output)
    win = MainWindow(cfg)
    if cfg.boot_screen:
        boot = BootScreen(win.boot_steps())

        def done():
            boot.close()
            win.show()
            _to_front(win)
        boot.finished.connect(done)
        boot.start()
        app._boot = boot
    else:
        win.show()
        _to_front(win)
        if cfg.auto_start:
            win.engine.start()
    sys.exit(app.exec())


def _run():
    """Windowless launches (pythonw) have no console, so errors go to deerdec_error.log + a dialog."""
    import os
    import traceback
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "deerdec_error.log"), "w") as f:
                f.write(tb)
            from PySide6.QtWidgets import QApplication, QMessageBox
            QApplication.instance() or QApplication([])
            QMessageBox.critical(None, "DeerDec crashed", tb[-1500:])
        except Exception:
            pass


if __name__ == "__main__":
    _run()
