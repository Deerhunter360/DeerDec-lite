"""Map tab: the live alert map inside DeerDec (Qt WebEngine), with an 'open in browser' fallback."""
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class MapTab(QWidget):
    def __init__(self, url_fn, parent=None):
        super().__init__(parent)
        self.url_fn, self.view, self.loaded = url_fn, None, False
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 6)
        self.info = QLabel("")
        self.info.setStyleSheet("color:#8a8f98;")
        reload_b = QPushButton("Reload")
        reload_b.clicked.connect(self.reload)
        browser_b = QPushButton("Open in browser")
        browser_b.clicked.connect(self._browser)
        bar.addWidget(self.info)
        bar.addStretch(1)
        bar.addWidget(reload_b)
        bar.addWidget(browser_b)
        v.addLayout(bar)
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            self.view = QWebEngineView()
            v.addWidget(self.view, 1)
        except Exception:
            msg = QLabel("The built-in map needs Qt WebEngine (part of PySide6 - run setup.bat again).\n\n"
                         "You can still use the map: press 'Open in browser'.")
            msg.setStyleSheet("padding:30px; color:#cfd3da; font-size:11pt;")
            v.addWidget(msg, 1)

    def showEvent(self, e):
        super().showEvent(e)
        if not self.loaded:
            self.reload()

    def reload(self):
        url = self.url_fn()
        self.info.setText(url)
        if self.view and url:
            self.view.setUrl(QUrl(url))
            self.loaded = True

    def _browser(self):
        import webbrowser
        url = self.url_fn()
        if url:
            webbrowser.open(url)
