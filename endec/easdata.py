import json

from .eventcodes import EVENT_FIXES


class EasData:
    """Mirrors determine_alert_color_and_title() from shared_utilities.py.
    EASData.json format: { "TOR": ["Tornado Warning", "<icon url prefix>"], ... }"""
    COLORS = {"unk": 0x797979, "adv": 0xffcc00, "wat": 0xff6600,
              "war": 0xff0000, "ean": 0x000000}

    def __init__(self, path="EASData.json"):
        try:
            with open(path) as f:
                self.urls = json.load(f)
        except Exception:
            self.urls = {}

    def resolve(self, eee, fallback_title=""):
        from . import defs
        entry = self.urls.get(eee)
        title = EVENT_FIXES.get(eee) or (entry[0] if entry else None) \
            or defs.SAME__EEE.get(eee) or fallback_title or f"Unknown Alert ({eee})"
        t = title.lower()
        c = self.COLORS
        if any(w in t for w in ("action", "center")):
            color = c["ean"]
        elif any(w in t for w in ("demo", "test", "advisory", "statement", "administrative",
                                  "practice", "transmitter", "network")):
            color = c["adv"]
        elif "watch" in t:
            color = c["wat"]
        elif any(w in t for w in ("warning", "emergency", "alert", "evacuation", "notification")):
            color = c["war"]
        else:
            color = c["unk"]
        image = (entry[1] + f"0x{color:06X}") if entry and len(entry) > 1 else ""
        return title, color, image, color == c["ean"]

    @classmethod
    def level(cls, color: int) -> str:
        """advisory (tests/advisories) / watch / alert - drives crawl colors."""
        if color == cls.COLORS["adv"]:
            return "advisory"
        if color == cls.COLORS["wat"]:
            return "watch"
        return "alert"
