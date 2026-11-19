from dataclasses import dataclass, field
from typing import List


@dataclass
class Alert:
    id: str = ""
    sender: str = ""
    sender_name: str = ""
    sent: str = ""
    expires: str = ""
    status: str = "Actual"
    msg_type: str = ""
    event: str = ""
    severity: str = ""
    headline: str = ""
    description: str = ""
    instruction: str = ""
    area: str = ""
    fips: List[str] = field(default_factory=list)
    eee: str = "EAN"
    org: str = "CIV"
    audio_b64: str = ""        # audio resource shipped inside the CAP message (base64)
    audio_uri: str = ""        # ...or a link to it
    audio_mime: str = ""
    cap_id: str = ""           # the CAP <identifier> (id gets replaced by the feed link)
    references: str = ""       # CAP <references>: alerts this one updates/cancels
    polygons: List[List[List[float]]] = field(default_factory=list)   # [[lat, lon], ...] rings
    ugc: List[str] = field(default_factory=list)       # NWS zone codes (county/marine), e.g. AMZ350
