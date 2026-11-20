"""Config-tab widgets: the county picker for the local area and the alert-filter table."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import defs, local

ACTION_ITEMS = [("normal", "Play, show and post as usual"), ("silent", "Show and post, but no audio"),
                ("discord only", "Only post to Discord"), ("no discord", "Play and show, do not post"),
                ("ignore", "Ignore it completely")]
WHERE_ITEMS = [("any", "Anywhere"), ("counties", "In the counties I choose"), ("local", "In my local area"),
               ("not local", "Outside my local area")]


def _split(s):
    return [x.strip().upper() for x in str(s or "").replace(";", ",").split(",") if x.strip()]


class CountyPicker(QWidget):
    """Tick the counties of your local area: pick a state, tick its counties; ticks are kept across states."""

    def __init__(self, selected):
        super().__init__()
        self.sel = set(selected or [])
        self.all = local.all_counties()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.state = QComboBox()
        for ss, name in sorted(((k, v_) for k, v_ in defs.US_SAME_AREA.items() if k != "LOCATION"), key=lambda x: x[1]):
            self.state.addItem(name, ss)
        self.find = QLineEdit()
        self.find.setPlaceholderText("search county…")
        row.addWidget(self.state)
        row.addWidget(self.find, 1)
        v.addLayout(row)
        self.lst = QListWidget()
        self.lst.setMinimumHeight(220)
        v.addWidget(self.lst)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#8a8f98; font-size:9pt;")
        v.addWidget(self.summary)
        btns = QHBoxLayout()
        for text, fn in (("Tick all shown", lambda: self._bulk(True)), ("Untick all shown", lambda: self._bulk(False)),
                         ("Clear everything", self._clear)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)
        self.state.currentIndexChanged.connect(self._fill)
        self.find.textChanged.connect(self._fill)
        self.lst.itemChanged.connect(self._changed)
        self._fill()

    def _fill(self):
        ss, q = self.state.currentData(), self.find.text().strip().lower()
        self.lst.blockSignals(True)
        self.lst.clear()
        for code, name in self.all:
            if code[:2] != ss or (q and q not in name.lower()):
                continue
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, code)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if code in self.sel else Qt.Unchecked)
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        self._sum()

    def _changed(self, it):
        code = it.data(Qt.UserRole)
        (self.sel.add if it.checkState() == Qt.Checked else self.sel.discard)(code)
        self._sum()

    def _bulk(self, on):
        self.lst.blockSignals(True)
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            (self.sel.add if on else self.sel.discard)(it.data(Qt.UserRole))
        self.lst.blockSignals(False)
        self._sum()

    def _clear(self):
        self.sel.clear()
        self._fill()

    def _sum(self):
        names = dict(self.all)
        n = sorted(self.sel)
        self.summary.setText(f"{len(n)} county(ies) in your local area" +
                             (": " + "; ".join(names.get(c, c) for c in n[:12]) + (" …" if len(n) > 12 else "") if n else ""))

    def get(self):
        return sorted(self.sel)

    def set(self, val):
        self.sel = set(val or [])
        self._fill()


class PickDialog(QDialog):
    """Tick any number of items from a searchable list."""

    def __init__(self, title, items, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 520)
        v = QVBoxLayout(self)
        self.find = QLineEdit()
        self.find.setPlaceholderText("search…")
        v.addWidget(self.find)
        self.lst = QListWidget()
        v.addWidget(self.lst, 1)
        for code, name in items:
            it = QListWidgetItem(f"{code} - {name}")
            it.setData(Qt.UserRole, code)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if code in selected else Qt.Unchecked)
            self.lst.addItem(it)
        self.find.textChanged.connect(self._filter)
        row = QHBoxLayout()
        none = QPushButton("Clear (= any)")
        none.clicked.connect(lambda: [self.lst.item(i).setCheckState(Qt.Unchecked) for i in range(self.lst.count())])
        ok = QPushButton("OK")
        ok.clicked.connect(self.accept)
        row.addWidget(none)
        row.addStretch(1)
        row.addWidget(ok)
        v.addLayout(row)

    def _filter(self, q):
        q = q.strip().lower()
        for i in range(self.lst.count()):
            self.lst.item(i).setHidden(bool(q) and q not in self.lst.item(i).text().lower())

    def chosen(self):
        return [self.lst.item(i).data(Qt.UserRole) for i in range(self.lst.count())
                if self.lst.item(i).checkState() == Qt.Checked]


class PickButton(QPushButton):
    """A drop-down style button: shows the current choice, click to tick events / origins."""

    def __init__(self, title, items, on_change):
        super().__init__("Any")
        self.title, self.items, self.on_change, self.codes = title, items, on_change, []
        self.setStyleSheet("text-align:left; padding:5px 8px;")
        self.clicked.connect(self._open)

    def _open(self):
        d = PickDialog(self.title, self.items, set(self.codes), self)
        if d.exec():
            self.set(d.chosen())
            self.on_change()

    def set(self, codes):
        self.codes = list(codes)
        self.setText("Any" if not codes else ", ".join(codes[:6]) + (f"  (+{len(codes) - 6})" if len(codes) > 6 else ""))


class CountyDialog(QDialog):
    def __init__(self, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose counties")
        self.resize(480, 520)
        v = QVBoxLayout(self)
        self.pick = CountyPicker(selected)
        v.addWidget(self.pick)
        ok = QPushButton("OK")
        ok.clicked.connect(self.accept)
        v.addWidget(ok)


class FilterTable(QWidget):
    """Alert filters, as a list you can re-order: the filter nearest the top that matches an alert is the one used."""

    def __init__(self, rows):
        super().__init__()
        self.rows, self._loading = [], False
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        left = QVBoxLayout()
        self.lst = QListWidget()
        self.lst.setMinimumHeight(190)
        self.lst.itemChanged.connect(self._ticked)
        self.lst.currentRowChanged.connect(self._load)
        left.addWidget(self.lst)
        top.addLayout(left, 1)
        side = QVBoxLayout()
        for text, fn in (("Add filter", self._add), ("Remove", self._remove), ("Move up", lambda: self._move(-1)),
                         ("Move down", lambda: self._move(1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            side.addWidget(b)
        side.addStretch(1)
        top.addLayout(side)
        v.addLayout(top)
        note = QLabel("The filter nearest the TOP that matches an alert is the one used - put the specific ones first.")
        note.setStyleSheet("color:#8a8f98; font-size:9pt;")
        v.addWidget(note)

        form = QFormLayout()
        self.name = QLineEdit()
        evs = sorted((k, n) for k, n in defs.SAME__EEE.items() if k)
        orgs = sorted((k, n['NAME']['US'].strip()) for k, n in defs.SAME__ORG.items() if k)
        self.events = PickButton("Event types", evs, self._edited)
        self.orgs = PickButton("Originators", orgs, self._edited)
        self.where = QComboBox()
        for key, text in WHERE_ITEMS:
            self.where.addItem(text, key)
        self.counties_btn = QPushButton("Choose counties…")
        self.counties_btn.clicked.connect(self._pick_counties)
        self.label = QLineEdit()
        self.label.setPlaceholderText("shown on Discord, e.g. LOCAL TORNADO")
        self.action = QComboBox()
        for key, text in ACTION_ITEMS:
            self.action.addItem(text, key)
        form.addRow("Name", self.name)
        form.addRow("Event types", self.events)
        form.addRow("Originator", self.orgs)
        form.addRow("Where", self.where)
        form.addRow("Counties", self.counties_btn)
        form.addRow("Label (Discord)", self.label)
        form.addRow("Then", self.action)
        self.form = QWidget()
        self.form.setLayout(form)
        v.addWidget(self.form)
        for w in (self.name, self.label):
            w.textEdited.connect(self._edited)
        for w in (self.where, self.action):
            w.currentIndexChanged.connect(self._edited)
        self.set(rows)

    # -- list <-> data
    def _summary(self, f):
        ev = ", ".join(_split(f.get("events"))[:4]) or "any event"
        where = dict(WHERE_ITEMS).get(f.get("where", "any"), "")
        if f.get("where") == "counties":
            where = f"{len(f.get('counties', []) or [])} chosen counties"
        return f"{f.get('name') or 'Filter'}  -  {ev}  -  {where}  ->  {dict(ACTION_ITEMS).get(f.get('action', 'normal'), '')}"

    def _refresh(self, keep=None):
        self._loading = True
        self.lst.clear()
        for i, f in enumerate(self.rows):
            it = QListWidgetItem(f"{i + 1}.  {self._summary(f)}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if f.get("enabled", True) else Qt.Unchecked)
            self.lst.addItem(it)
        self._loading = False
        if self.rows:
            self.lst.setCurrentRow(min(len(self.rows) - 1, max(0, keep if keep is not None else 0)))
        self.form.setEnabled(bool(self.rows))
        if not self.rows:
            self._load(-1)

    def _ticked(self, it):
        if not self._loading:
            self.rows[self.lst.row(it)]["enabled"] = it.checkState() == Qt.Checked

    def _load(self, r):
        self._loading = True
        f = self.rows[r] if 0 <= r < len(self.rows) else {}
        self.name.setText(f.get("name", ""))
        self.events.set(_split(f.get("events")))
        self.orgs.set(_split(f.get("orgs")))
        self.where.setCurrentIndex(max(0, self.where.findData(f.get("where", "any"))))
        self.label.setText(f.get("label", ""))
        n = len(f.get("counties", []) or [])
        self.counties_btn.setText(f"Choose counties…  ({n} chosen)" if n else "Choose counties…")
        self.counties_btn.setEnabled(f.get("where") == "counties")
        self.action.setCurrentIndex(max(0, self.action.findData(f.get("action", "normal"))))
        self._loading = False

    def _pick_counties(self):
        r = self.lst.currentRow()
        if not (0 <= r < len(self.rows)):
            return
        d = CountyDialog(self.rows[r].get("counties", []), self)
        if d.exec():
            self.rows[r]["counties"] = d.pick.get()
            self.rows[r]["where"] = "counties"
            self._load(r)
            self._edited()

    def _edited(self, *_):
        r = self.lst.currentRow()
        if self._loading or not (0 <= r < len(self.rows)):
            return
        f = self.rows[r]
        self.counties_btn.setEnabled(self.where.currentData() == "counties")
        f.update(name=self.name.text().strip(), events=",".join(self.events.codes), orgs=",".join(self.orgs.codes),
                 where=self.where.currentData(), label=self.label.text().strip(), action=self.action.currentData())
        self._loading = True
        it = self.lst.item(r)
        it.setText(f"{r + 1}.  {self._summary(f)}")
        self._loading = False

    def _add(self):
        self.rows.append({"name": "New filter", "enabled": True, "events": "", "orgs": "", "where": "any",
                          "label": "", "action": "normal"})
        self._refresh(len(self.rows) - 1)

    def _remove(self):
        r = self.lst.currentRow()
        if 0 <= r < len(self.rows):
            del self.rows[r]
            self._refresh(r)

    def _move(self, d):
        r = self.lst.currentRow()
        n = r + d
        if 0 <= r < len(self.rows) and 0 <= n < len(self.rows):
            self.rows[r], self.rows[n] = self.rows[n], self.rows[r]
            self._refresh(n)

    def get(self):
        return [dict(f) for f in self.rows]

    def set(self, rows):
        self.rows = [dict(f) for f in (rows or [])]
        self._refresh(0)
