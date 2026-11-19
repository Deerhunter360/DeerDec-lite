import urllib.request
import xml.etree.ElementTree as ET
from typing import List
from .models import Alert

UA = {"User-Agent": "Mozilla/5.0 (personal-endec/2.0)"}


def _ln(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _ln(c.tag) == name:
            return c
    return None


def _text(el, name, default=""):
    c = _child(el, name)
    return (c.text or "").strip() if c is not None and c.text else default


def _pairs(el, group):
    out = {}
    for g in el:
        if _ln(g.tag) == group:
            out.setdefault(_text(g, "valueName"), []).append(_text(g, "value"))
    return out


class CapParser:
    """IPAWS/CAP XML (Atom-wrapped or bare) -> Alert objects."""

    def parse(self, xml_bytes: bytes) -> List[Alert]:
        root = ET.fromstring(xml_bytes)
        out = []
        for el in root.iter():
            if _ln(el.tag) == "alert":
                a = self._alert(el)
                if a:
                    out.append(a)
        return out

    def _alert(self, el):
        info = _child(el, "info")
        if info is None:
            return None
        params = _pairs(info, "parameter")
        codes, areas, polys, ugcs = [], [], [], []
        for area in info:
            if _ln(area.tag) == "area":
                areas.append(_text(area, "areaDesc"))
                gc = _pairs(area, "geocode")
                codes += gc.get("SAME", [])
                ugcs += gc.get("UGC", [])
                for pg in area:
                    if _ln(pg.tag) == "polygon" and pg.text:
                        try:
                            ring = [[float(v) for v in pt.split(",")[:2]] for pt in pg.text.split() if "," in pt]
                            if len(ring) >= 3:
                                polys.append(ring)
                        except ValueError:
                            pass
        a_b64 = a_uri = a_mime = ""
        for res in info:
            if _ln(res.tag) == "resource":
                mime, desc = _text(res, "mimeType"), _text(res, "resourceDesc")
                if mime.lower().startswith("audio") or "audio" in desc.lower() or "eas broadcast" in desc.lower():
                    a_b64, a_uri, a_mime = _text(res, "derefUri"), _text(res, "uri"), mime
                    break
        return Alert(
            audio_b64=a_b64, audio_uri=a_uri, audio_mime=a_mime,
            ugc=list(dict.fromkeys(ugcs)),
            cap_id=_text(el, "identifier"), references=_text(el, "references"), polygons=polys,
            id=_text(el, "identifier"), sender=_text(el, "sender"),
            sent=_text(el, "sent"), status=_text(el, "status"),
            msg_type=_text(el, "msgType"), event=_text(info, "event"),
            severity=_text(info, "severity"), headline=_text(info, "headline"),
            description=_text(info, "description"),
            instruction=_text(info, "instruction"),
            sender_name=_text(info, "senderName"), expires=_text(info, "expires"),
            area="; ".join(a for a in areas if a),
            fips=list(dict.fromkeys(codes)),
            eee=(_pairs(info, "eventCode").get("SAME") or ["EAN"])[0],
            org=(params.get("EAS-ORG") or ["CIV"])[0][:3].upper(),
        )


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class IpawsClient:
    """IPAWS-OPEN EAS feed is an Atom INDEX: each <entry> links to the full CAP record
    (.../rest/eas/<id>). Alert.id is the entry link, so it dedupes across polls."""

    def __init__(self, url: str, parser: CapParser = None, log=print):
        self.url, self.log = url, log
        self.parser = parser or CapParser()
        self.last_total = 0
        self.last_links = []

    @staticmethod
    def _entries(root):
        out = []
        for el in root.iter():
            if _ln(el.tag) != "entry":
                continue
            link, cats = "", {}
            for c in el:
                n = _ln(c.tag)
                if n == "link":
                    link = c.get("href") or link
                elif n == "id" and not link:
                    link = (c.text or "").strip()
                elif n == "category":
                    cats[c.get("label", "")] = c.get("term", "")
                elif n == "updated":
                    cats["_updated"] = (c.text or "").strip()
            if link:
                out.append((link, cats))
        return out

    def detail(self, link, quiet=False):
        """Full CAP record for an index-only alert (None if it can't be fetched)."""
        try:
            found = self.parser.parse(_get(link))
            if found:
                found[0].id = link
                return found[0]
        except Exception as e:
            if not quiet:
                body = ""
                try:
                    body = e.read(300).decode("utf-8", "ignore").strip().replace("\n", " ")
                except Exception:
                    pass
                self.log(f"IPAWS: could not fetch {link} for the map: {e}" + (f" - server said: {body}" if body else ""))
        return None

    def fetch(self, seen=(), details=True) -> List[Alert]:
        """Alerts for entries not in `seen` (all entries if the feed is plain CAP)."""
        raw = _get(self.url)
        root = ET.fromstring(raw)
        entries = self._entries(root)
        if not entries:                                    # plain CAP / unknown layout
            alerts = self.parser.parse(raw)
            self.last_total = len(alerts)
            return alerts
        self.last_total = len(entries)
        self.last_links = [link for link, _ in entries]
        out = []
        for link, cats in entries:
            if link in seen:
                continue
            alert = None
            if details:
                try:
                    found = self.parser.parse(_get(link))
                    alert = found[0] if found else None
                except Exception as e:
                    self.log(f"IPAWS: could not fetch {link.rsplit('/', 1)[-1]}: {e}")
            if alert is None:                              # index data only
                ss = cats.get("statefips", "").zfill(2)
                alert = Alert(sent=cats.get("_updated", ""), status="Actual", event="",
                              eee=cats.get("event", "EAN") or "EAN", org="CIV",
                              fips=[f"0{ss}000"] if ss.strip("0") else ["000000"],
                              area=f"State FIPS {ss}")
            alert.id = link
            out.append(alert)
        return out
