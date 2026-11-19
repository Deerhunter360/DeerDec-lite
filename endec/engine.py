import collections
import json
import numpy as np
import urllib.error
import urllib.parse
import urllib.request
import os
import queue
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .cap import IpawsClient
from .config import Config
from .easdata import EasData
from .models import Alert
from .same import AfskEncoder, SameHeader
from .tts import Tts

STATE_FILE = "seen.json"
LOG_FILE = "alert_log.json"


@dataclass
class AlertRecord:
    alert: Alert
    header: str                 # raw ZCZC string
    text: str                   # "EAS Text Data" for Discord/TTS (follows selected style)
    title: str                  # e.g. "Tornado Warning"
    color: int
    is_national: bool
    wav: Optional[str] = None
    seconds: float = 0.0
    voice_text: str = ""
    paras: List[str] = field(default_factory=list)   # DASDEC slide 1
    extra: List[str] = field(default_factory=list)   # DASDEC later slides (message)
    text_classic: str = ""                           # EASyPLUS-wording sentence
    level: str = "alert"                             # advisory / watch / alert
    areas: List[str] = field(default_factory=list)   # display names, one per area
    end_local: str = ""                              # "9:45 PM"
    end24: str = ""                                  # "21:45"
    station: str = ""                                # "Message from ..." name
    message: str = ""                                # alert message (CAP text / typed)
    from_audio: bool = False                         # heard on an audio source (wav = recorded audio)
    mode: str = "off"                                # audio: off / record / live
    ready: bool = True                               # False until a record-mode recording has finished
    log_id: str = ""                                 # entry in the alert log


def fips_match(alert_codes, watch):
    if not watch or "000000" in watch:
        return True
    for c in alert_codes:
        if c == "000000":
            return True
        for w in watch:
            if c[1:] == w[1:]:
                return True
            if (c[3:] == "000" or w[3:] == "000") and c[1:3] == w[1:3]:   # statewide either way
                return True
    return False


class Endec:
    """Poll loop + audio monitor + pipeline. Talks to the GUI via self.events:
    ('log', str) / ('status', str) / ('alert', AlertRecord)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events: "queue.Queue" = queue.Queue()
        self.encoder = AfskEncoder()
        self.tts = Tts()
        self._stop = threading.Event()
        self._wake = threading.Event()             # set to poll IPAWS right now
        self._want_ipaws = self._want_audio = False
        self._thread: Optional[threading.Thread] = None
        self.seen = self._load_seen()
        self.alert_log = self._load_log()
        self._map_cache = {}                         # feed link -> full Alert
        self._zone_geo = None
        self._outline_info = {}                     # alert id -> text borrowed from the matching NWS / CAP alert
        self._outline_tried = set()                 # CAP alerts already being searched for an outline
        self._heard_poly = {}                        # heard-alert id -> polygon rings borrowed from NWS
        self._nws_geo = None
        self._map_src = {}                           # raw header -> (Alert, source name) heard on audio
        self._map_built = 0.0
        self.map_lock = threading.Lock()
        self.map_alerts, self.map_hidden = [], 0
        self._recent = {}
        self.log_buf, self.log_seq = collections.deque(maxlen=600), 0    # recent log lines for the web page
        self.building = 0                            # alerts being built right now
        self.gui_call = None                         # set by the main window: run a function on the GUI thread
        self._hdr_log = []                           # (key, issued, origin, time) of handled alerts
        self._audio_recs = {}                      # raw header -> time (dedupe across audio sources)
        self.ipaws = {"state": "Not started", "polls": 0, "last": None, "alerts": 0,
                      "new": 0, "error": "", "next": None}
        self.ipaws_src = {}                          # feed name -> {state, error, fails}
        self._map_feed = {}                          # alert link -> name of the feed it came from
        self.started_at = None
        try:
            from .monitor import MonitorManager
            self.monitor = MonitorManager(self)
        except ImportError:                    # numpy missing
            self.monitor = None

    # ---------------------------------------------------------------- state
    def _load_seen(self):
        try:
            return set(json.load(open(STATE_FILE)))
        except Exception:
            return set()

    def _log_silent(self, a, source):
        """Put an alert in the Alert Log without showing/playing/posting it."""
        try:
            hdr = SameHeader.from_alert(a, self.cfg.callsign)
            rec = self.preview_record(a, hdr)
            self._log_alert(rec, a, source)
            self.events.put(("logged", None))
        except Exception as e:
            self.log(f"Could not log alert: {e}")

    # ------------------------------------------------------------ alert map data
    MAP_EXCLUDE = {"RWT", "DMO"}                      # weekly tests and demos never go on the map

    def _refresh_map(self, clients):
        """Keep full CAP records for everything in the feeds and work out which are still active."""
        owner = {}
        for c in clients:
            for link in (getattr(c, "last_links", []) or []):
                owner[link] = c
        links = list(owner)
        fails = self.__dict__.setdefault("_map_fail", {})        # link -> (tries, retry-after time)
        now = datetime.now(timezone.utc)
        for k in [k for k, al in self._map_cache.items()          # forget alerts that expired long ago
                  if k not in owner and (self._parse_time(al.expires) or now) < now - timedelta(hours=1)]:
            del self._map_cache[k]
        for link in links:
            self._map_feed[link] = getattr(owner[link], "feed_name", "IPAWS")
            if link in self._map_cache:
                continue
            tries, after = fails.get(link, (0, 0))
            if tries >= 3 or time.time() < after:                 # gave up on it / waiting to retry
                continue
            full = owner[link].detail(link, quiet=tries > 0)
            if full:
                self._map_cache[link] = full
                fails.pop(link, None)
            else:
                fails[link] = (tries + 1, time.time() + 120 * (tries + 1))
            time.sleep(0.15)                                      # be gentle with the servers
        for link in list(fails):
            if link not in links:
                del fails[link]
        self._resolve_zones()
        self._resolve_nws()
        self._start_cap_outlines()
        polled = {getattr(c, "feed_name", "IPAWS") for c in clients}
        for link in list(self._map_cache):                         # gone from a feed that answered = gone
            if link not in owner and self._map_feed.get(link, "IPAWS") in polled:
                del self._map_cache[link]
        self._rebuild_map()

    MARINE = {"AM", "AN", "GM", "LC", "LE", "LH", "LM", "LO", "LS", "PH", "PK", "PM", "PS", "PZ", "SL"}
    # SAME marine area (0SSCCC) -> NWS zone prefix: 075454 = AMZ454
    SAME_MARINE = {"57": "PZ", "58": "PK", "59": "PH", "61": "PS", "65": "PM", "73": "AN", "75": "AM",
                   "77": "GM", "91": "LS", "92": "LM", "93": "LH", "94": "LC", "96": "LE", "97": "LO", "98": "SL"}

    STATE_ABBR = {
        "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
        "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
        "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
        "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
        "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
        "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP",
        "72": "PR", "78": "VI"}

    def _heard_lookup(self, raw, hdr, key=None, alive=None):
        """An NWR alert heard on a stream (or a CAP alert that came without a polygon): find the same alert on api.weather.gov and borrow its polygon
        (or the outlines of its zones). Only improves the map shape - it never creates an alert."""
        areas = set()
        for c in hdr.fips:
            c = str(c).zfill(6)
            if c[1:3] in self.SAME_MARINE:
                areas.add(self.SAME_MARINE[c[1:3]])
            elif c[1:3] in self.STATE_ABBR:
                areas.add(self.STATE_ABBR[c[1:3]])
        areas.discard("")
        if not areas or len(areas) > 6:
            return
        fips = {str(c).zfill(6) for c in hdr.fips}
        key = key or f"audio-{raw}"
        where = "heard on the stream" if key.startswith("audio-") else "from the CAP feed"
        alive = alive or (lambda: raw in self._map_src)
        why = "no attempt"
        try:                                                   # keep looking until the alert ends (max 3 h)
            deadline = hdr.issued + timedelta(hours=int(hdr.purge[:2]), minutes=int(hdr.purge[2:]))
        except Exception:
            deadline = hdr.issued + timedelta(hours=1)
        deadline = min(deadline, datetime.now(timezone.utc) + timedelta(hours=3))
        waits = [0, 20, 30, 45, 90, 120]
        n = -1
        while True:
            n += 1
            wait = waits[n] if n < len(waits) else 60          # NWS / IPAWS can take minutes to list it
            if self._stop.wait(wait) or not alive() or datetime.now(timezone.utc) > deadline:
                break
            # 1) an IPAWS / CAP alert for the same thing may have arrived since - borrow its polygon
            twin = self._cap_twin(hdr)
            if twin:
                self._heard_poly[key] = twin.polygons
                self._outline_info[key] = {"headline": twin.headline, "description": twin.description,
                                           "instruction": twin.instruction, "severity": twin.severity,
                                           "from": "the CAP feed"}
                self.log(f"Map: found the outline and text for the {hdr.eee} {where} in the CAP feed ({len(twin.polygons)} shape(s))")
                self._rebuild_map()
                return
            try:
                feats = self._nws_get("https://api.weather.gov/alerts/active?area=" + ",".join(sorted(areas))).get("features", [])
            except Exception as e:
                why = f"could not reach the NWS alerts API ({e})"
                continue
            cands, same_event, seen = [], 0, {}
            for f in feats:
                pr = f.get("properties", {})
                for code in (pr.get("eventCode", {}).get("SAME") or ["?"]):
                    seen[code] = seen.get(code, 0) + 1
                if hdr.eee not in (pr.get("eventCode", {}).get("SAME") or []):
                    continue
                same_event += 1
                same = {str(x).zfill(6) for x in (pr.get("geocode", {}).get("SAME") or [])}
                overlap = len(same & fips)
                if not overlap:
                    continue
                t = self._parse_time(pr.get("sent") or pr.get("effective"))
                gap = abs((t - hdr.issued).total_seconds()) if t else 0
                cands.append((gap > 30 * 60, -overlap, gap, f))
            why = (f"{len(feats)} active NWS alert(s) in {','.join(sorted(areas))}, {same_event} {hdr.eee}, "
                   f"{len(cands)} covering the same area"
                   + ("" if same_event or not seen else " [NWS lists: " + ", ".join(f"{k} {v}" for k, v in sorted(seen.items(), key=lambda x: -x[1])[:8]) + "]"))
            if n == 0:
                self.log(f"Map: looking for the NWS outline of the {hdr.eee} {where} - {why} "
                         "(will keep checking NWS and the CAP feeds until it shows up)")
            elif n % 5 == 0:
                self.log(f"Map: still waiting for the {hdr.eee} outline ({why})")
            cands.sort(key=lambda c: c[:3])
            best = cands[0][3] if cands and not cands[0][0] else None
            if not best:
                continue
            pr = best.get("properties", {})
            self._outline_info[key] = {"headline": pr.get("headline") or "", "description": pr.get("description") or "",
                                       "instruction": pr.get("instruction") or "", "severity": pr.get("severity") or "",
                                       "from": "the National Weather Service"}
            self._rebuild_map()                                 # the text can show even before the shape is found
            rings = self._rings(best.get("geometry"))
            if not rings:
                try:
                    for zurl in (best.get("properties", {}).get("affectedZones") or [])[:25]:
                        rings += self._rings(self._nws_get(zurl).get("geometry"))
                except Exception:
                    pass
            if rings:
                self._heard_poly[key] = rings
                self.log(f"Map: found the NWS outline for the {hdr.eee} {where} ({len(rings)} shape(s))")
                self._rebuild_map()
                return
            why = "found the alert but NWS has no polygon or zone shapes for it"
        if alive():
            self.log(f"Map: no outline for the {hdr.eee} {where} ({why}) - shown by county")

    def _start_cap_outlines(self):
        """A CAP alert that arrived with no polygon (and isn't an NWS alert we can look up by id) gets the same
        patient search as a heard alert: NWS first, other CAP feeds too, retrying until it shows up."""
        now = datetime.now(timezone.utc)
        sup = self._superseded(self._map_cache.values())
        for a in list(self._map_cache.values()):
            if (a.polygons or not a.fips or a.id in self._outline_tried or a.id in self._heard_poly
                    or (a.cap_id or "").startswith("urn:oid:2.49.0.1.840.0")
                    or a.eee in self.TEST_CODES or not self._map_eligible(a, now, sup)):
                continue
            if len(self._outline_tried) > 500:
                self._outline_tried.clear()
            self._outline_tried.add(a.id)
            try:
                hdr = SameHeader.from_alert(a, self.cfg.callsign)
                hdr.issued = self._parse_time(a.sent) or hdr.issued
            except Exception:
                continue
            ids = a.id
            alive = lambda ids=ids: any(x.id == ids for x in list(self._map_cache.values()))
            threading.Thread(target=self._heard_lookup, args=(a.id, hdr, a.id, alive), daemon=True).start()

    def _cap_twin(self, hdr):
        """Polygon rings of a CAP alert (any feed) with the same event covering the same counties, if there is one."""
        fips = {str(c).zfill(6) for c in hdr.fips}
        for a in list(self._map_cache.values()):
            if a.eee != hdr.eee or not a.polygons:
                continue
            if not ({str(c).zfill(6) for c in a.fips} & fips):
                continue
            t = self._parse_time(a.sent)
            if t and abs((t - hdr.issued).total_seconds()) > 20 * 60:
                continue                                   # a different warning for the same area
            return a
        return None

    def _ugcs(self, a):
        """NWS zone codes for an alert: the ones in its CAP plus any marine area derived from its SAME codes."""
        out = list(a.ugc)
        for c in a.fips:
            c = str(c).zfill(6)
            pre = self.SAME_MARINE.get(c[1:3])
            if pre:
                out.append(f"{pre}Z{c[3:]}")
        return list(dict.fromkeys(out))

    def _zone_cache(self):
        if self._zone_geo is None:
            try:
                with open(os.path.join("data", "zones.json"), encoding="utf-8") as f:
                    self._zone_geo = json.load(f)
            except Exception:
                self._zone_geo = {}
        return self._zone_geo

    def _resolve_zones(self):
        """Marine zones (waters) have no county outline: fetch their shapes from the NWS zone API, once each."""
        geo = self._zone_cache()
        todo = []
        for a in list(self._map_cache.values()) + [x for x, _s in self._map_src.values()]:
            for u in self._ugcs(a):
                if len(u) == 6 and u[:2] in self.MARINE and u[2] == "Z" and u not in geo and u not in todo:
                    todo.append(u)
        changed = False
        for u in todo[:12]:                                   # a few per poll
            rings = None
            for kind in ("coastal", "offshore", "marine", "forecast"):
                try:
                    req = urllib.request.Request(f"https://api.weather.gov/zones/{kind}/{u}",
                                                 headers={"User-Agent": "DeerDec/1.0", "Accept": "application/geo+json"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        g = json.loads(r.read()).get("geometry") or {}
                    polys = g.get("coordinates") or []
                    if g.get("type") == "Polygon":
                        polys = [polys]
                    rings = []
                    for poly in polys:
                        if poly:
                            ring = [[c[1], c[0]] for c in poly[0]]
                            step = max(1, len(ring) // 500)
                            rings.append(ring[::step])
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        continue
                    break
                except Exception:
                    break
            if rings is not None:
                geo[u] = rings
                changed = True
            else:
                self.log(f"Map: could not get the outline of marine zone {u}")
                geo[u] = []                                   # do not hammer the API for one that fails
                changed = True
        if changed:
            try:
                os.makedirs("data", exist_ok=True)
                with open(os.path.join("data", "zones.json"), "w", encoding="utf-8") as f:
                    json.dump(geo, f)
            except OSError:
                pass

    def _superseded(self, alerts):
        sup = set()
        for a in alerts:
            for ref in (a.references or "").split():
                parts = ref.split(",")
                if len(parts) >= 2:
                    sup.add(parts[1])
        return sup

    def _map_eligible(self, a, now, superseded):
        """Only alerts that were received, are valid (not cancelled/replaced) and not expired are mapped."""
        if a.msg_type == "Cancel" or a.eee in self.MAP_EXCLUDE:
            return False
        if a.eee not in ("RMT", "NPT") and (a.status or "Actual") != "Actual":
            return False                                   # tests are only mapped for RMT / NPT
        if a.cap_id and a.cap_id in superseded:
            return False
        exp = self._parse_time(a.expires)
        return not (exp and exp < now)

    # ---- polygons from NWS for IPAWS alerts that came without one (never creates alerts)
    def _nws_cache(self):
        if self._nws_geo is None:
            try:
                with open(os.path.join("data", "nws_polys.json"), encoding="utf-8") as f:
                    self._nws_geo = json.load(f)
            except Exception:
                self._nws_geo = {}
        return self._nws_geo

    @staticmethod
    def _rings(geom):
        g = geom or {}
        polys = g.get("coordinates") or []
        if g.get("type") == "Polygon":
            polys = [polys]
        out = []
        for poly in polys:
            if poly:
                ring = [[c[1], c[0]] for c in poly[0]]
                out.append(ring[::max(1, len(ring) // 500)])
        return out

    _nws_cache_t = {}                                       # url -> (time, data): many alerts ask for the same list

    @classmethod
    def _nws_get(cls, url):
        hit = cls._nws_cache_t.get(url)
        if hit and time.time() - hit[0] < 45:
            return hit[1]
        req = urllib.request.Request(url, headers={"User-Agent": "DeerDec/1.0", "Accept": "application/geo+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        if len(cls._nws_cache_t) > 60:
            cls._nws_cache_t.clear()
        cls._nws_cache_t[url] = (time.time(), data)
        return data

    def _resolve_nws(self):
        now = datetime.now(timezone.utc)
        sup = self._superseded(self._map_cache.values())
        geo, changed = self._nws_cache(), False
        for a in list(self._map_cache.values()):
            if a.polygons or not a.cap_id or not a.cap_id.startswith("urn:oid:2.49.0.1.840.0"):
                continue                                    # has its own polygon / not an NWS alert
            if a.cap_id in geo or not self._map_eligible(a, now, sup):
                continue
            try:
                feat = self._nws_get("https://api.weather.gov/alerts/" + urllib.parse.quote(a.cap_id, safe=""))
                rings = self._rings(feat.get("geometry"))
                if not rings:                               # no polygon: use the outlines of the zones it lists
                    for zurl in (feat.get("properties", {}).get("affectedZones") or [])[:25]:
                        rings += self._rings(self._nws_get(zurl).get("geometry"))
                geo[a.cap_id] = rings
                if rings:
                    self.log(f"Map: got NWS outline for {a.eee} ({len(rings)} shape(s))")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    geo[a.cap_id] = []
                else:
                    continue
            except Exception:
                continue
            changed = True
        for k in [k for k in geo if k not in {x.cap_id for x in self._map_cache.values()}]:
            del geo[k]                                      # forget alerts that left the feed
            changed = True
        if changed:
            try:
                os.makedirs("data", exist_ok=True)
                with open(os.path.join("data", "nws_polys.json"), "w", encoding="utf-8") as f:
                    json.dump(geo, f)
            except OSError:
                pass

    def _rebuild_map(self):
        now = datetime.now(timezone.utc)
        alerts = list(self._map_cache.values())
        if self.cfg.map_show_sources:
            alerts += [al for al, _src in self._map_src.values()]     # heard on streams / inputs
        sources = {al.id: src for al, src in self._map_src.values()}
        ipaws_keys = {(a.eee, frozenset(a.fips)) for a in self._map_cache.values()}
        superseded = self._superseded(alerts)
        easdata = EasData(self.cfg.easdata_path)
        out, hidden = [], 0
        for a in alerts:
            if not self._map_eligible(a, now, superseded):
                hidden += 1
                continue
            if not a.fips and not a.polygons and not a.ugc:
                continue
            if a.id in sources and (a.eee, frozenset(a.fips)) in ipaws_keys:
                continue                                    # IPAWS already has this one, with more detail
            zones = [ring for u in self._ugcs(a) for ring in self._zone_cache().get(u, [])]
            polys = a.polygons or (self._nws_cache().get(a.cap_id) if a.cap_id else None) or self._heard_poly.get(a.id, [])
            title, color, _img, _n = easdata.resolve(a.eee, a.event)
            info = self._outline_info.get(a.id) or {}
            out.append({
                "zones": zones,
                "id": a.id, "eee": a.eee, "title": title, "level": EasData.level(color),
                "sender": a.sender_name or a.sender, "area": a.area, "fips": a.fips,
                "polygons": polys, "headline": info.get("headline") or a.headline,
                "description": self._with_note(a, info, a.id in sources),
                "instruction": a.instruction or info.get("instruction", ""), "severity": a.severity or info.get("severity", ""),
                "sent": self._fmt_time(a.sent), "expires": self._fmt_time(a.expires),
                "origin": ("Heard on " + sources[a.id]) if a.id in sources else self._map_feed.get(a.id, "IPAWS"),
                "expired": False, "status": a.status, "msg_type": a.msg_type,
                "expires_ts": exp.timestamp() if (exp := self._parse_time(a.expires)) else None,
            })
        out.sort(key=lambda d: {"alert": 0, "watch": 1, "advisory": 2}.get(d["level"], 3))
        with self.map_lock:
            self.map_alerts, self.map_hidden = out, hidden

    @staticmethod
    def _with_note(a, info, heard):
        """Message text for the map card: the alert's own, else text borrowed from the matching NWS / CAP alert."""
        got = (info or {}).get("description", "").strip()
        if not got:
            return a.description
        note = f"(Message text from {info.get('from', 'another source')}.)"
        if heard:                                          # replace the 'only the header is known' placeholder
            raw = a.description.rsplit("\n\n", 1)[-1]
            return f"{got}\n\n{note}\nHeard on the audio source: {raw}"
        return a.description if a.description.strip() else f"{got}\n\n{note}"

    def map_add_heard(self, hdr, raw, source):
        """An alert heard on an audio source goes on the map too (SAME header data only)."""
        try:
            exp = hdr.issued + timedelta(hours=int(hdr.purge[:2]), minutes=int(hdr.purge[2:]))
            al = Alert(id=f"audio-{raw}", status="Actual", eee=hdr.eee, org=hdr.org, fips=list(hdr.fips),
                       area=hdr.area_text(), sender_name=hdr.callsign, sent=hdr.issued.isoformat(),
                       expires=exp.isoformat(), headline=f"Heard on {source}",
                       description=f"Received from the audio source '{source}'. Only the SAME header is "
                                   f"available for these, so there is no polygon, message text or instructions.\n\n{raw}")
            self._map_src[raw] = (al, source)
            now = datetime.now(timezone.utc)                   # forget expired ones
            for k in [k for k, (x, _s) in self._map_src.items()
                      if (self._parse_time(x.expires) or now) < now - timedelta(hours=1)]:
                del self._map_src[k]
            while len(self._map_src) > 200:
                del self._map_src[next(iter(self._map_src))]
            self._rebuild_map()
            if hdr.org in ("WXR", "EAS") and hdr.eee not in self.TEST_CODES:     # stations relay NWS warnings too
                threading.Thread(target=self._heard_lookup, args=(raw, hdr), daemon=True).start()
            if any(c[1:3] in self.SAME_MARINE for c in (str(f).zfill(6) for f in hdr.fips)):
                threading.Thread(target=lambda: (self._resolve_zones(), self._rebuild_map()), daemon=True).start()
        except Exception as e:
            self.log(f"Map: could not add heard alert: {e}")

    def map_payload(self):
        if time.time() - self._map_built > 5:                     # expired alerts leave the map between polls too
            self._map_built = time.time()
            self._rebuild_map()
        with self.map_lock:
            return {"updated": time.strftime("%H:%M:%S"), "alerts": list(self.map_alerts),
                    "hidden": self.map_hidden}

    @staticmethod
    def _parse_time(s):
        try:
            t = datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @classmethod
    def _fmt_time(cls, s):
        t = cls._parse_time(s)
        return t.astimezone().strftime("%b %d, %I:%M %p").replace(" 0", " ") if t else (s or "")

    def _load_log(self):
        try:
            return json.load(open(LOG_FILE, encoding="utf-8"))
        except Exception:
            return []

    def _log_alert(self, rec, a, source):
        from dataclasses import asdict
        entry = {"id": f"{time.time():.3f}", "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "source": source or "", "header": rec.header, "title": rec.title, "eee": a.eee,
                 "org": a.org, "level": rec.level, "areas": rec.areas, "text": rec.text,
                 "message": rec.message, "wav": rec.wav, "from_audio": rec.from_audio,
                 "alert": {k: v for k, v in asdict(a).items() if not k.startswith("audio_")}}
        self.alert_log.append(entry)
        del self.alert_log[:-500]
        rec.log_id = entry["id"]
        self._save_log()

    def _save_log(self):
        try:
            json.dump(self.alert_log, open(LOG_FILE, "w", encoding="utf-8"), indent=1)
        except Exception as e:
            self.log(f"Could not save the alert log: {e}")

    def _update_log_wav(self, log_id, path):
        for e in self.alert_log:
            if e["id"] == log_id:
                e["wav"] = path
                self._save_log()
                return

    def rec_from_entry(self, entry):
        """Rebuild the on-screen record for a logged alert (no audio, no Discord)."""
        a = Alert(**{k: v for k, v in entry["alert"].items() if k in Alert.__dataclass_fields__})
        hdr = SameHeader.parse(entry["header"]) or SameHeader.from_alert(a, self.cfg.callsign)
        return a, hdr, self.preview_record(a, hdr)



    def _save_seen(self):
        json.dump(sorted(self.seen), open(STATE_FILE, "w"))

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log_seq += 1
        self.log_buf.append((self.log_seq, line))
        self.events.put(("log", line))

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------------------------------------------- lifecycle
    def start_ipaws(self):
        if self.running:
            return
        self._stop.clear()
        self._want_ipaws = True
        self.started_at = self.started_at or time.time()
        self.ipaws.update(state="Starting…", error="")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.events.put(("status", "RUNNING"))

    def start_audio(self):
        self._want_audio = True
        if self.monitor:
            self.monitor.start()
        else:
            self.log("Audio monitoring unavailable: pip install numpy sounddevice")

    def start(self):
        self.start_ipaws()
        self.start_audio()

    def replay_feed(self):
        """Fetch what is in the IPAWS feed right now and show/play all of it (screen + audio only,
        no Discord), ignoring the 'already seen' list. Handy for testing."""
        def work():
            try:
                self.log("Replaying the current IPAWS feed…")
                for src in self._ipaws_sources():
                    client = IpawsClient(src["target"], log=self.log)
                    alerts = [a for a in client.fetch(set(), details=True) if a.id]
                    self.log(f"IPAWS replay [{src['name']}]: {len(alerts)} alert(s) in the feed")
                    for a in alerts:
                        self.process(a, notify=False, check_dup=False, feed=src["name"])
            except Exception as e:
                self.log(f"IPAWS replay failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        """Reconnect every stream/input and poll IPAWS immediately."""
        self.log("Refreshing connections…")
        self.ipaws["fails"] = 0
        self._wake.set()
        if self.monitor and self._want_audio:
            self.monitor.restart_all()

    def supervise(self):
        """Called every few seconds by the GUI: bring back a poller/monitor thread that died."""
        if self._want_ipaws and not self.running:
            self.log("IPAWS poller stopped unexpectedly - restarting")
            self.start_ipaws()
        if self._want_audio and self.monitor and not self.monitor.running:
            self.log("Audio monitor stopped unexpectedly - restarting")
            self.monitor.start()

    def stop(self):
        self._want_ipaws = self._want_audio = False
        self._stop.set()
        self._wake.set()
        if self.monitor:
            self.monitor.stop()
        self.ipaws.update(state="Stopped", next=None)
        self.events.put(("status", "STOPPED"))

    def matches(self, a: Alert) -> bool:
        if self.cfg.only_actual and a.status != "Actual":
            return False
        return fips_match(a.fips, self.cfg.watch_fips)

    def _ipaws_enabled(self):
        return bool(self._ipaws_sources())

    def _ipaws_sources(self):
        return [x for x in self.cfg.sources
                if x.get("kind") == "ipaws" and x.get("enabled", True) and x.get("target")]

    # ---------------------------------------------------------------- pipeline
    DUP_WINDOW_S = 900            # how long a handled alert is remembered (seconds)
    DUP_ISSUE_MIN = 5             # same alert if the issue times are within this many minutes

    def _dup_of(self, hdr, issued, origin):
        """Same alert already handled? Everything in the header must match EXCEPT the callsign (and the
        issue time may differ a little: IPAWS 'sent' vs the broadcaster's own header time).
        Returns the origin of the earlier copy, or None - and remembers this one."""
        now = time.time()
        self._hdr_log = [h for h in self._hdr_log if now - h[3] < self.DUP_WINDOW_S]
        key = (hdr.eee, tuple(sorted(hdr.fips)), hdr.purge)
        for k, t, org, _ts in self._hdr_log:
            if k == key and abs((t - issued).total_seconds()) <= self.DUP_ISSUE_MIN * 60:
                return org
        self._hdr_log.append((key, issued, origin, now))
        return None

    def process(self, a: Alert, notify=True, check_dup=True, feed="IPAWS") -> Optional[AlertRecord]:
        hdr = SameHeader.from_alert(a, self.cfg.callsign)
        if check_dup:
            issued = self._parse_time(a.sent) or hdr.issued
            first = self._dup_of(hdr, issued, feed)
            if first:
                self.log(f"{feed}: {a.eee} is the same alert already handled from {first} "
                         f"(only the callsign differs) - not played again")
                return None
        return self.build(a, hdr, notify, source=f"Pulled from {feed}" if feed != "IPAWS" else "Pulled from IPAWS CAP")

    def process_decoded(self, raw: str, source: str, notify: bool = True, force: bool = False):
        """A confirmed SAME header heard on an audio source."""
        hdr = SameHeader.parse(raw)
        if not hdr:
            self.log(f"Ignored invalid header from {source}: {raw}")
            return None
        now = time.time()
        self._recent = {k: t for k, t in self._recent.items() if now - t < 300}
        if raw in self._recent:
            self.log(f"Duplicate header from {source} ignored (identical to one handled "
                     f"{int(now - self._recent[raw])}s ago)")
            self._audio_recs.pop(source, None)
            return None
        self._recent[raw] = now
        if not force:
            first = self._dup_of(hdr, hdr.issued, source)
            if first:
                self.log(f"Heard {hdr.eee} on {source}: same alert already handled from {first} "
                         f"(only the callsign differs) - ignored")
                self._audio_recs.pop(source, None)
                return None
        if not force:
            self.map_add_heard(hdr, raw, source)
        if not force and not fips_match(hdr.fips, self.cfg.watch_fips):
            self.log(f"Heard {hdr.eee} from {source} - outside watched locations")
            self._audio_recs.pop(source, None)
            return None
        a = Alert(id=f"audio-{raw}", status="Actual", eee=hdr.eee, org=hdr.org, fips=hdr.fips,
                  area=hdr.area_text(), sender_name=hdr.callsign, sent=hdr.issued.isoformat(),
                  expires=hdr.end_local().isoformat())
        mode = self.monitor.decide_mode(source) if self.monitor else "off"
        if self.monitor:                                  # start the live gate NOW - building the alert takes a while
            self.monitor.accepted(source, mode)
        rec = self.build(a, hdr, notify=notify, tts=False, from_audio=True, mode=mode,
                         source=f"Received from {source} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._audio_recs[source] = rec
        return rec

    def relay_done(self, source: str, path: str, seconds: float):
        """The audio that followed a heard header has been recorded to `path`."""
        rec = self._audio_recs.pop(source, None)
        if rec:
            rec.wav, rec.seconds, rec.ready = path, seconds, True
            if rec.log_id:
                self._update_log_wav(rec.log_id, path)
        else:                                         # never became an alert (outside area / duplicate / noise)
            try:
                os.remove(path)
            except OSError:
                pass
        self.events.put(("relay", (source, path, seconds, rec)))

    TEST_CODES = {"RWT", "RMT", "NPT", "DMO"}

    @classmethod
    def _restates(cls, extra: str, text: str, eee: str) -> bool:
        """True if the alert's own description just repeats what the generated text already says
        (CAP feeds often send 'a Required Weekly Test was issued for ...' as the description).
        Short messages someone typed in on purpose (e.g. 'this is a test') are always kept."""
        import re
        words = lambda t: {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 3}
        e, t = words(extra), words(text)
        if not e:
            return False
        ratio = len(e & t) / len(e)
        if eee in cls.TEST_CODES:                         # tests: only drop long boilerplate descriptions
            return len(extra.split()) >= 15 and ratio >= 0.3
        return ratio >= 0.5

    def preview_record(self, a: Alert, hdr: SameHeader) -> AlertRecord:
        """Record for the CGEN only - no audio, no Discord."""
        raw = hdr.encode()
        paras = hdr.dasdec_paragraphs()
        classic = hdr.classic_text()
        title, color, _img, national = EasData(self.cfg.easdata_path).resolve(a.eee, a.event)
        text = " ".join(paras) if self.cfg.cgen_style == "dasdec" else classic
        extra = [" ".join(x.split()) for x in (a.description, a.instruction) if x and x.strip()]
        extra = [x for x in extra if not self._restates(x, text, a.eee)]      # no reading the same thing twice
        end = hdr.end_local()
        return AlertRecord(
            a, raw, text, title, color, national, None, 0.0, "", paras, extra, classic,
            EasData.level(color), hdr.area_names(), end.strftime("%I:%M %p").lstrip("0"),
            end.strftime("%H:%M"), hdr.station, " ".join(extra))

    def build(self, *args, **kw) -> AlertRecord:
        """Counts alerts being built so the audio switch can cut program audio the moment one is accepted."""
        self.building += 1
        try:
            return self._build(*args, **kw)
        finally:
            self.building -= 1

    def _build(self, a: Alert, hdr: SameHeader, notify=False, source=None,
               tts: bool = True, from_audio: bool = False, mode: str = "off") -> AlertRecord:
        os.makedirs(self.cfg.out_dir, exist_ok=True)
        rec = self.preview_record(a, hdr)
        voice_text, voice = "", None
        if tts and self.cfg.use_cap_audio and (a.audio_b64 or a.audio_uri):
            from .audio import load_cap_audio
            voice = load_cap_audio(a, self.encoder.sr, self.cfg.ffmpeg_path, self.log)
            if voice:
                voice_text = "(audio supplied with the alert)"
                self.log(f"Using the audio supplied with the alert ({len(voice) / self.encoder.sr:.0f}s)")
        if voice is None and tts:
            if not (a.audio_b64 or a.audio_uri):
                self.log("The alert has no audio - generating a voice")
        if voice is None and self.cfg.tts_enabled and tts:
            parts = [rec.text] + ([rec.message] if self.cfg.tts_include_description else [])
            voice_text = " ".join(" ".join(parts).replace("/", " ").split())
            voice = self.tts.synthesize(voice_text, self.encoder.sr, self.cfg.tts_rate,
                                        self.cfg.tts_voice, self.log)
        if voice and self.cfg.level_correct:
            from .levels import normalize
            voice = normalize(np.asarray(voice, dtype=np.float64), self.encoder.sr,
                              self.cfg.level_target_db, self.cfg.level_max_gain_db).tolist()
        rec.wav = os.path.join(self.cfg.out_dir, f"{int(time.time())}_{a.eee}.wav")
        rec.voice_text, rec.from_audio, rec.mode = voice_text, from_audio, mode
        rec.ready = mode != "record"
        self.encoder.render(rec.header, rec.wav, self.cfg.attn_seconds, voice)
        try:
            with wave.open(rec.wav) as w:
                rec.seconds = w.getnframes() / w.getframerate()
        except Exception:
            pass
        self._log_alert(rec, a, source)
        self.events.put(("alert", rec))
        return rec

    # ---------------------------------------------------------------- IPAWS loop
    @staticmethod
    def _age_minutes(sent):
        """Minutes since an ISO timestamp (None if it can't be read)."""
        try:
            t = datetime.fromisoformat(sent.strip().replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
        except Exception:
            return None

    def _poll_feed(self, src, first):
        """One poll of one CAP feed: returns (client, new_count). Raises on failure."""
        name = src["name"]
        label = "IPAWS" if len(self._ipaws_sources()) == 1 else f"IPAWS [{name}]"
        client = IpawsClient(src["target"], log=self.log)
        client.feed_name = name
        new = [a for a in client.fetch(self.seen, details=not first) if a.id]
        todo = new
        if first:                              # smart first poll: recent alerts yes, old backlog no
            mins, keep = self.cfg.first_poll_recent_minutes, []
            for a in new:
                self.seen.add(a.id)
                age = self._age_minutes(a.sent)
                if mins > 0 and age is not None and age <= mins:
                    keep.append(client.detail(a.id) or a)
            todo = keep
            self.log(f"{label} polled: {client.last_total} in feed, {len(new)} new - showing "
                     f"{len(keep)} from the last {mins} min, {len(new) - len(keep)} older skipped")
        else:
            self.log(f"{label} polled: {client.last_total} in feed, {len(new)} new")
        for a in todo:
            self.seen.add(a.id)
            if not self.matches(a):
                self.log(f"{label}: skipped {a.eee} ({a.status}) - 'Only Actual alerts' is on")
                continue
            lim, age = self.cfg.ipaws_play_max_minutes, self._age_minutes(a.sent)
            if lim > 0 and age is not None and age > lim:
                self.log(f"{label}: {a.eee} was sent {age:.0f} min ago - older than {lim} min, logged but not played")
                self._log_silent(a, f"{label} (sent {age:.0f} min ago - not played)")
                continue
            self.process(a, feed=name if label != "IPAWS" else "IPAWS")
        return client, len(new)

    def _loop(self):
        firsts = {}                                    # feed name -> has had its first poll
        while not self._stop.is_set():
            ip = self.ipaws
            srcs = self._ipaws_sources()
            if not srcs:
                ip.update(state="Disabled", next=None)
                self._stop.wait(2)
                continue
            ip.update(state="Polling…")
            clients, errors, total, newn, worst = [], [], 0, 0, 0
            for src in srcs:
                name = src["name"]
                st = self.ipaws_src.setdefault(name, {"state": "", "error": "", "fails": 0})
                st.update(state="Polling…")
                try:
                    client, n = self._poll_feed(src, firsts.setdefault(name, self.cfg.skip_backlog))
                    firsts[name] = False
                    clients.append(client)
                    total, newn = total + client.last_total, newn + n
                    st.update(state="OK", error="", fails=0)
                except Exception as e:
                    st["fails"] = st.get("fails", 0) + 1
                    st.update(state=f"ERROR - retrying (try {st['fails']})", error=str(e)[:90])
                    errors.append(f"{name}: {str(e)[:70]}")
                    self.log(f"IPAWS poll error [{name}]: {e}")
                worst = max(worst, st.get("fails", 0))
            try:
                self._save_seen()
                if clients:
                    self._refresh_map(clients)
            except Exception as e:
                self.log(f"Map refresh problem: {e}")
            if clients:
                ip.update(state="OK" if not errors else "OK (some feeds failing)", polls=ip["polls"] + 1,
                          last=time.time(), alerts=total, new=newn, error="; ".join(errors))
            else:
                ip.update(state=f"ERROR - retrying (try {worst})", error="; ".join(errors), last=time.time())
            ip["fails"] = worst
            # after a failure retry sooner than the normal interval (10 s, 20 s, 40 s ... up to poll_seconds)
            delay = self.cfg.poll_seconds
            if worst:
                delay = min(delay, 10 * 2 ** min(worst - 1, 4))
            ip["next"] = time.time() + delay
            if not self._stop.is_set():
                self._wake.wait(delay)
            self._wake.clear()
        self.ipaws.update(state="Stopped", next=None)
