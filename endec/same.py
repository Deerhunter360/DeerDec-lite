import math
import re
import struct
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from . import defs
from .eventcodes import EVENT_FIXES
from .models import Alert

defs.SAME__EEE.update(EVENT_FIXES)


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def purge_from_times(sent: str, expires: str) -> str:
    try:
        mins = max(1, int((_parse_dt(expires) - _parse_dt(sent)).total_seconds() // 60))
    except Exception:
        mins = 60
    step = 15 if mins <= 60 else 30
    mins = min(-(-mins // step) * step, 99 * 60 + 59)
    return f"{mins // 60:02d}{mins % 60:02d}"


ORG_DASDEC = {
    "PEP": ("THE PRIMARY ENTRY POINT EAS SYSTEM", False),
    "CIV": ("THE CIVIL AUTHORITIES", True),
    "WXR": ("THE NATIONAL WEATHER SERVICE", False),
    "EAS": ("AN EAS PARTICIPANT", False),
}

# Used by CEMS-1000 / PSC-902 style screens
ORG_LONG = {
    "PEP": "The Primary Entry Point System",
    "CIV": "A civil authority",
    "WXR": "The National Weather Service",
    "EAS": "A Broadcast station or cable system",
}

STATE_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}


def _a_an(word: str) -> str:
    return "an" if word and word[0].lower() in "aeiou" else "a"


@dataclass
class SameHeader:
    org: str = "EAS"
    eee: str = "RWT"
    fips: List[str] = field(default_factory=lambda: ["000000"])
    purge: str = "0030"                      # HHMM
    issued: Optional[datetime] = None        # aware UTC
    callsign: str = "DEERDEC"
    raw: Optional[str] = None                # original text when decoded from audio

    def __post_init__(self):
        if self.issued is None:
            self.issued = datetime.now(timezone.utc)
        self.fips = [c.zfill(6) for c in (self.fips or ["000000"])][:31]

    # ---------------------------------------------------------------- basics
    @property
    def call8(self) -> str:
        """Callsign exactly as typed (max 8 chars) - no '/' padding."""
        c = self.callsign.strip().upper().replace("-", "/")[:8].rstrip("/ ")
        return c or "ENDEC"

    @property
    def station(self) -> str:
        """Callsign as shown on screens: 'WBAP/1' -> 'WBAP 1'."""
        return self.call8.replace("/", " ").strip()

    def encode(self) -> str:
        if self.raw:
            return self.raw
        return (f"ZCZC-{self.org}-{self.eee}-{'-'.join(self.fips)}"
                f"+{self.purge}-{self.issued.strftime('%j%H%M')}-{self.call8}-")

    def end_local(self) -> datetime:
        return (self.issued + timedelta(hours=int(self.purge[:2]),
                                        minutes=int(self.purge[2:]))).astimezone()

    SAME_RE = re.compile(r"^ZCZC-([A-Z0-9]{3})-([A-Z0-9]{3})-((?:\d{6}-){0,30}\d{6})"
                         r"\+(\d{4})-(\d{7})-([^-]{1,8})-$")

    @classmethod
    def parse(cls, raw: str) -> Optional["SameHeader"]:
        """Raw 'ZCZC-...' string (e.g. from the audio decoder) -> SameHeader, or None."""
        m = cls.SAME_RE.match(raw.strip())
        if not m:
            return None
        org, eee, locs, purge, jjjhhmm, call = m.groups()
        jjj, hh, mm = int(jjjhhmm[:3]), int(jjjhhmm[3:5]), int(jjjhhmm[5:7])
        if not (1 <= jjj <= 366 and hh < 24 and mm < 60 and int(purge[2:]) < 60):
            return None
        now = datetime.now(timezone.utc)
        year = now.year
        issued = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=jjj - 1, hours=hh, minutes=mm)
        if issued > now + timedelta(days=2):
            issued = datetime(year - 1, 1, 1, tzinfo=timezone.utc) + timedelta(days=jjj - 1, hours=hh, minutes=mm)
        return cls(org, eee, locs.split("-"), purge, issued, call, raw=raw.strip())

    @classmethod
    def from_alert(cls, a: Alert, callsign: str) -> "SameHeader":
        purge = purge_from_times(a.sent, a.expires) if a.expires else "0100"
        return cls(a.org, a.eee, a.fips, purge, None, callsign)

    # ---------------------------------------------------------------- areas
    def area_groups(self):
        """[[state_name, 'ALL' | [county names], SS], ...]; 000000 -> ['United States','ALL','00']."""
        groups, index = [], {}
        for code in self.fips:
            if code == "000000":
                groups.append(["United States", "ALL", "00"])
                continue
            SS, CCC, SSCCC = code[1:3], code[3:], code[1:]
            state = defs.US_SAME_AREA.get(SS, "Unknown")
            entry = index.get(SS)
            if entry is None:
                entry = [state, [], SS]
                index[SS] = entry
                groups.append(entry)
            if CCC == "000":
                entry[1] = "ALL"
            elif entry[1] != "ALL":
                loc = defs.SAME_LOCA.get(code[0])
                name = defs.US_SAME_CODE.get(SSCCC, SSCCC)
                entry[1].append(f"{loc} {name}" if loc else name)
        return groups

    def area_names(self) -> List[str]:
        """One display name per area: 'United States', 'All of Alabama', 'Kanawha', ..."""
        out = []
        for state, val, _ss in self.area_groups():
            if val == "ALL":
                out.append("United States" if state == "United States" else f"All of {state}")
            else:
                out += val
        return out

    def area_text(self) -> str:
        """'Kanawha, WV; Putnam, WV; All of Ohio' style summary."""
        out = []
        for state, val, ss in self.area_groups():
            if val == "ALL":
                out.append("United States" if state == "United States" else f"All of {state}")
            else:
                out += [f"{c}, {STATE_ABBR.get(ss, ss)}" for c in val]
        return "; ".join(out)

    # ---------------------------------------------------------------- DASDEC wording
    def dasdec_paragraphs(self) -> List[str]:
        org, plural = ORG_DASDEC.get(self.org, (f"THE {self.org}", False))
        event = (defs.SAME__EEE.get(self.eee) or self.eee).upper()
        art = "AN" if event[:1] in "AEIOU" else "A"
        items = []
        for state, val, ss in self.area_groups():
            if val == "ALL":
                items.append("United States;" if state == "United States" else f"All of {state};")
            else:
                items += [f"{c}, {STATE_ABBR.get(ss, ss)};" for c in val]
        start, end = self.issued.astimezone(), self.end_local()
        fmt_t = lambda d: d.strftime("%I:%M %p").lstrip("0")
        return [
            f"{org} {'have' if plural else 'has'} issued {art} {event} "
            f"for the following counties or areas:",
            *items,                              # one area per line, like the real screen
            f"at {fmt_t(start)}",
            f"on {start.strftime('%b').upper()} {start.day}, {start.year}",
            f"Effective until {fmt_t(end)}.",
            f"Message from {self.station}.",
        ]

    def digital_text(self) -> str:
        return " ".join(self.dasdec_paragraphs())

    readable = digital_text

    # ---------------------------------------------------------------- EASyPLUS wording
    def classic_text(self) -> str:
        """'The National Weather Service has issued a Tornado Warning valid until 4:57 AM for
        the following counties in West Virginia: Kanawha, Putnam. Message from WFQTWVKT.'"""
        org = defs.SAME__ORG.get(self.org)
        if org:
            name = org["NAME"]["US"].strip()
            article = {"A": "A", "THE": "The"}.get(org["ARTICLE"]["US"], "The")
            has = "have" if org["PLURAL"] else "has"
        else:
            name, article, has = self.org, "The", "has"
        event = defs.SAME__EEE.get(self.eee) or self.eee
        end = self.end_local().strftime("%I:%M %p").lstrip("0")
        parts = []
        for state, val, ss in self.area_groups():
            if state == "United States":
                parts.append("the United States")
            elif val == "ALL":
                parts.append(f"all of {state}")
            else:
                div = defs.FIPS_DIVN.get(ss, "counties") or "areas"
                parts.append(f"the following {div} in {state}: {', '.join(val)}")
        return (f"{article} {name} {has} issued {_a_an(event)} {event} valid until {end} "
                f"for {' and '.join(parts)}. Message from {self.station}.")


class AfskEncoder:
    """Renders SAME headers + attention tone to a mono 16-bit WAV."""
    MARK, SPACE, BAUD = 2083.3333, 1562.5, 520.8333

    def __init__(self, sample_rate: int = 44100):
        self.sr = sample_rate

    def _afsk(self, data: bytes):
        out, phase, acc = [], 0.0, 0.0
        spb = self.sr / self.BAUD
        for byte in data:
            for i in range(8):                      # LSB first
                f = self.MARK if (byte >> i) & 1 else self.SPACE
                acc += spb
                n = int(acc)
                acc -= n
                step = 2 * math.pi * f / self.sr
                for _ in range(n):
                    out.append(math.sin(phase))
                    phase += step
        return out

    def _silence(self, sec):
        return [0.0] * int(self.sr * sec)

    def _attn(self, sec):
        return [0.5 * (math.sin(2 * math.pi * 853 * i / self.sr) +
                       math.sin(2 * math.pi * 960 * i / self.sr))
                for i in range(int(self.sr * sec))]

    def render(self, header: str, path: str, attn_sec: float = 8.0,
               voice: Optional[List[float]] = None) -> str:
        burst = self._afsk(b"\xAB" * 16 + header.encode("ascii"))
        eom = self._afsk(b"\xAB" * 16 + b"NNNN")
        s = []
        for _ in range(3):
            s += burst + self._silence(1)
        if attn_sec > 0:
            s += self._attn(attn_sec)
        s += self._silence(1) + (voice or []) + self._silence(1)
        for _ in range(3):
            s += eom + self._silence(1)
        pcm = b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 0.8 * 32767)) for x in s)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(pcm)
        return path
