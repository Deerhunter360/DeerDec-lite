"""Locations (FIPS picker) and Sources (inputs) tabs."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QSlider, QTableWidget,
                               QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import defs
from .same import STATE_ABBR  # noqa: F401  (keeps defs patches applied)

TERRITORIES = {"60", "64", "66", "68", "69", "70", "72", "74", "78"}
MARINE = {"57", "58", "59", "61", "65", "73", "75", "77",
          "91", "92", "93", "94", "96", "97", "98"}


# =====================================================================  LOCATIONS


# =====================================================================  SOURCES
KIND_LABEL = {"ipaws": "IPAWS CAP feed", "stream": "Audio stream", "device": "Audio input device"}


class SourcesTab(QWidget):
    """Inputs: IPAWS feed, audio streams and sound-card inputs. Every enabled audio source is
    decoded for SAME headers at all times; 'Listen' only controls whether you can HEAR one."""

    def __init__(self, cfg, engine, on_change=None, on_volume=None):
        super().__init__()
        self.cfg, self.engine, self.on_change, self.on_volume = cfg, engine, on_change, on_volume
        v = QVBoxLayout(self)
        box = QGroupBox("Currently selected input")
        bl = QVBoxLayout(box)
        self.current = QLabel("")
        f = QFont()
        f.setPointSize(13)
        f.setBold(True)
        self.current.setFont(f)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        bl.addWidget(self.current)
        bl.addWidget(self.detail)
        v.addWidget(box)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Active", "On", "Listen", "Name", "Type", "Source", "Status", "Level"])
        hh = self.table.horizontalHeader()
        for c in (0, 1, 2, 3, 4, 6):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        hh.setSectionResizeMode(7, QHeaderView.Fixed)
        self.table.setColumnWidth(7, 130)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemChanged.connect(self._toggled)
        v.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, fn in (("Add CAP feed…", self._add_feed), ("Edit…", self._edit), ("Remove", self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addWidget(QLabel("Volume"))
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(120)
        self.vol.valueChanged.connect(lambda x: self.on_volume and self.on_volume(x / 100.0))
        row.addWidget(self.vol)
        row.addStretch()
        v.addLayout(row)
        v.addWidget(QLabel("All enabled audio sources are decoded silently for EAS headers. "
                           "Select one and press Listen to hear it; press again to mute."))
        self._reload()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_status)
        self.timer.start(700)

    # ---- model helpers
    def _sel(self):
        r = self.table.currentRow()
        return r if 0 <= r < len(self.cfg.sources) else None

    def _commit(self):
        self.cfg.save()
        if self.on_change:
            self.on_change()
        self._reload()

    def _mon(self, s):
        m = self.engine.monitor
        return (m.status.get(s["name"]) if m else None) or {}

    def _status(self, s):
        if not s.get("enabled", True):
            return "Disabled"
        if s["kind"] == "ipaws":
            st = self.engine.ipaws_src.get(s["name"])
            if st:
                return st["state"] + (f" ({st['error']})" if st["error"] else "")
            return self.engine.ipaws["state"]
        if not self.engine.monitor:
            return "Needs numpy (pip install numpy sounddevice)"
        st = self._mon(s).get("state")
        return st or ("Starting…" if self.engine.running else "Stopped (press Start)")

    def _level(self, s):
        lv = self._mon(s).get("level", 0.0) if s["kind"] != "ipaws" else 0.0
        n = min(10, int(lv * 14))
        return "█" * n + "·" * (10 - n) if s["kind"] != "ipaws" else ""

    def _reload(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        listen = self.engine.monitor.listen_name if self.engine.monitor else None
        for s in self.cfg.sources:
            r = self.table.rowCount()
            self.table.insertRow(r)
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            on.setCheckState(Qt.Checked if s.get("enabled", True) else Qt.Unchecked)
            vals = ["◉" if s["name"] == self.cfg.active_source else "", None,
                    "🔊" if s["name"] == listen else "", s["name"],
                    KIND_LABEL.get(s["kind"], s["kind"]), s["target"], self._status(s), self._level(s)]
            for c, val in enumerate(vals):
                if c == 1:
                    self.table.setItem(r, c, on)
                else:
                    it = QTableWidgetItem(val)
                    it.setTextAlignment(Qt.AlignCenter if c in (0, 2) else Qt.AlignLeft | Qt.AlignVCenter)
                    self.table.setItem(r, c, it)
        self.table.blockSignals(False)
        self._refresh_current()

    def _refresh_current(self):
        act = next((s for s in self.cfg.sources if s["name"] == self.cfg.active_source), None)
        if act:
            self.current.setText(f"{act['name']}  —  {KIND_LABEL.get(act['kind'], act['kind'])}")
            last = self._mon(act).get("last", "") if act["kind"] != "ipaws" else ""
            self.detail.setText(f"{act['target']}\nStatus: {self._status(act)}"
                                + (f"\nLast header: {last}" if last else ""))
        else:
            self.current.setText("No input selected")
            self.detail.setText("Pick a source below and press “Set as active”.")

    def _refresh_status(self):
        for r, s in enumerate(self.cfg.sources):
            for c, val in ((6, self._status(s)), (7, self._level(s))):
                it = self.table.item(r, c)
                if it:
                    it.setText(val)
        self._refresh_current()

    # ---- actions
    def _toggled(self, item):
        if item.column() == 1 and 0 <= item.row() < len(self.cfg.sources):
            self.cfg.sources[item.row()]["enabled"] = item.checkState() == Qt.Checked
            self._commit()

    def _set_active(self):
        r = self._sel()
        if r is not None:
            self.cfg.active_source = self.cfg.sources[r]["name"]
            self._commit()



    def _unique(self, name):
        names = {s["name"] for s in self.cfg.sources}
        base, i = name, 2
        while name in names:
            name, i = f"{base} ({i})", i + 1
        return name


    def _add_feed(self):
        url, ok = QInputDialog.getText(self, "Add CAP feed", "IPAWS-style Atom feed URL:")
        if not ok or not url.strip():
            return
        name, ok = QInputDialog.getText(self, "Add CAP feed", "Name:", text=url.strip().split("/")[2])
        if ok and name.strip():
            self.cfg.sources.append({"name": self._unique(name.strip()), "kind": "ipaws",
                                     "target": url.strip(), "enabled": True})
            self._commit()

    def _first_ipaws(self, s):
        return next((x for x in self.cfg.sources if x["kind"] == "ipaws"), None) is s


    def _edit(self):
        r = self._sel()
        if r is None:
            return
        s = self.cfg.sources[r]
        name, ok = QInputDialog.getText(self, "Edit input", "Name:", text=s["name"])
        if not ok or not name.strip():
            return
        label = "Feed URL:" if s["kind"] == "ipaws" else "Stream URL:" if s["kind"] == "stream" else "Device:"
        target, ok = QInputDialog.getText(self, "Edit input", label, text=s["target"])
        if not ok or not target.strip():
            return
        was_active = s["name"] == self.cfg.active_source
        s["name"] = name.strip() if name.strip() == s["name"] else self._unique(name.strip())
        s["target"] = target.strip()
        if was_active:
            self.cfg.active_source = s["name"]
        if s["kind"] == "ipaws" and self._first_ipaws(s):
            self.cfg.feed_url = s["target"]
        self._commit()

    def _remove(self):
        r = self._sel()
        if r is None:
            return
        s = self.cfg.sources[r]
        if s["kind"] == "ipaws" and self._first_ipaws(s):
            QMessageBox.information(self, "Remove", "The main IPAWS feed can be disabled but not removed.")
            return
        del self.cfg.sources[r]
        if self.cfg.active_source == s["name"]:
            self.cfg.active_source = self.cfg.sources[0]["name"] if self.cfg.sources else ""
        if self.engine.monitor and self.engine.monitor.listen_name == s["name"]:
            self.engine.monitor.set_listen(None)
        self._commit()


class AlertLogTab(QWidget):
    """Every alert DeerDec has handled (kept in alert_log.json). Pick one to see it, play it,
    or send it again."""
    COLS = ["Time", "Source", "Event", "Originator", "Level", "Areas"]

    def __init__(self, engine, on_show, on_play, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QPlainTextEdit
        self.engine, self.on_show, self.on_play = engine, on_show, on_play
        self.rows = []
        v = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        for i, w in enumerate((150, 220, 220, 110, 80)):
            self.table.setColumnWidth(i, w)
        self.table.itemSelectionChanged.connect(self._detail)
        v.addWidget(self.table, 3)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(150)
        v.addWidget(self.detail, 1)
        row = QHBoxLayout()
        for text, fn in (("Show on screen", self._show), ("Play audio", self._play),
                         ("Send old alert…", self._send), ("Refresh", self.reload)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        self.count = QLabel("")
        row.addWidget(self.count)
        v.addLayout(row)
        self.reload()

    def reload(self, *_):
        sel = self._sel_entry()
        self.rows = list(reversed(self.engine.alert_log))            # newest first
        self.table.setRowCount(len(self.rows))
        for r, e in enumerate(self.rows):
            vals = (e["time"], (e.get("source") or "")[:80], e["title"], e["org"], e.get("level", ""),
                    "; ".join(e.get("areas", []))[:200])
            for c, val in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(val))
            if sel and e["id"] == sel["id"]:
                self.table.selectRow(r)
        self.count.setText(f"{len(self.rows)} logged")

    def _sel_entry(self):
        r = self.table.currentRow() if self.table.selectedItems() else -1
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def _detail(self):
        e = self._sel_entry()
        if not e:
            self.detail.setPlainText("")
            return
        wav = e.get("wav") or ""
        have = "yes" if wav and __import__("os").path.exists(wav) else "no"
        self.detail.setPlainText(f"{e['header']}\n\n{e['text']}\n" + (f"\nMessage: {e['message']}\n" if e.get("message") else "")
                                 + f"\nSource: {e.get('source', '')}\nAudio saved: {have}")

    def _need(self):
        e = self._sel_entry()
        if e is None:
            QMessageBox.information(self, "Alert log", "Select an alert in the list first.")
        return e

    def _show(self):
        e = self._need()
        if e:
            self.on_show(e)

    def _play(self):
        e = self._need()
        if not e:
            return
        wav = e.get("wav") or ""
        if wav and __import__("os").path.exists(wav):
            self.on_play(wav)
        else:
            QMessageBox.information(self, "Alert log", "There is no saved audio for this alert. "
                                    "'Send old alert' will generate it again.")

    def _send(self):
        e = self._need()
        if not e:
            return
        ans = QMessageBox.question(
            self, "Send old alert",
            f"Are you sure you want to send this old alert?\n\n{e['title']}\nLogged {e['time']}\n\n"
            "It will be shown on the alert screen and played.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans == QMessageBox.Yes:
            self.engine.resend(e)
