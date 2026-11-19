import json
from dataclasses import dataclass, field, asdict
from typing import List

CONFIG_FILE = "config.json"
STYLE_ALIASES = {"classic": "easyplus", "digital": "dasdec"}


@dataclass
class Config:
    # --- feed
    feed_url: str = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/feed"
    poll_seconds: int = 60
    watch_fips: List[str] = field(default_factory=list)   # unused: DeerDec takes alerts for every area
    only_actual: bool = True
    skip_backlog: bool = True
    first_poll_recent_minutes: int = 10   # on the first poll still show alerts issued this recently (0 = none)
    ipaws_play_max_minutes: int = 10      # IPAWS alerts older than this are logged but not shown/played (0 = no limit)
    audio_output: str = ""                # speaker/device for alert audio and Listen ("" = system default)
    map_show_rmt: bool = True             # draw Required Monthly Tests on the map
    map_show_sources: bool = True         # alerts heard on streams/inputs go on the map too
    map_debug_all: bool = False           # debug: show everything (any type/status, expired, no area)
    level_correct: bool = True            # even out loud/quiet speech
    level_target_db: int = -16            # target speech level (dBFS RMS)
    level_max_gain_db: int = 18           # never boost more than this
    map_port: int = 8765                  # local alert map: http://127.0.0.1:<port>/
    # --- SAME / audio
    callsign: str = "DEERDEC"
    attn_seconds: int = 8
    out_dir: str = "alerts"
    easdata_path: str = "EASData.json"
    # --- inputs (Sources tab)
    sources: List[dict] = field(default_factory=lambda: [
        {"name": "IPAWS CAP feed", "kind": "ipaws", "enabled": True,
         "target": "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/feed"},
        {"name": "Global EAS (local CAP)", "kind": "ipaws", "enabled": True,
         "target": "https://alerts.globaleas.org/IPAWSOPEN_EAS_SERVICE/rest/feed"}])
    active_source: str = "IPAWS CAP feed"
    ffmpeg_path: str = "ffmpeg"
    window_geometry: str = ""          # main window size/position (saved on exit)
    # --- heard alerts: what to do with the audio that follows a decoded header
    audio_relay: str = "record"        # record = capture, then play when it ends (EASyPLUS style)
                                       # live   = play while it is still coming in
                                       # off    = decode only
    relay_max_seconds: int = 180       # safety cap if the EOM is never heard
    # --- startup
    auto_start: bool = True            # start IPAWS polling + audio monitoring on launch
    boot_screen: bool = True           # show the DeerDec startup sequence
    # --- CGEN screen
    auto_cgen: bool = True
    auto_play: bool = True             # play alert audio automatically
    use_cap_audio: bool = True         # use the audio inside the IPAWS alert when it has some
    cfg_version: int = 4
    cgen_seconds: int = 60
    cgen_style: str = "dasdec"         # see cgen.STYLES
    cgen_screen: int = -1              # monitor index (-1 = default)
    laptop_only: bool = False          # LAPTOP ONLY: alert + details screens in a window, not full screen
    bulletin_station: str = "KDEER 12"   # Weather Bulletin style: call letters + channel number
    cgen_page_seconds: float = 8.0
    details_channel: bool = False      # idle "EAS DETAILS CHANNEL" screen, full screen, with alerts shown over it
    details_text: str = "EAS DETAILS CHANNEL"            # EASyPLUS standby text
    details_text_dasdec: str = "DEERDEC Details Channel"  # DASDEC standby text (the unit shows its own name here)
    details_text_vds: str = "DETAILS CHANNEL"            # VDS standby second line
    cgen_font: str = "EAS OSD Mono"
    classic_scale: float = 1.0
    atari_scale: float = 1.0           # Atari 800XL text size multiplier
    # --- voice (offline TTS, for alerts that arrive without audio)
    tts_enabled: bool = True
    tts_rate: int = 165
    tts_voice: str = ""
    tts_include_description: bool = True

    @classmethod
    def load(cls, path=CONFIG_FILE):
        try:
            with open(path) as f:
                data = json.load(f)
            cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            if data.get("cfg_version", 1) < 2:      # v2: alert audio plays by default
                cfg.auto_play, cfg.cfg_version = True, 2
            if data.get("cfg_version", 1) < 3:      # v3: 10-minute window for IPAWS alerts
                cfg.first_poll_recent_minutes, cfg.cfg_version = 10, 3
            if data.get("cfg_version", 1) < 4:      # v4: second CAP feed (Global EAS), can be switched off in Sources
                if not any("globaleas.org" in x.get("target", "") for x in cfg.sources):
                    cfg.sources.insert(1, {"name": "Global EAS (local CAP)", "kind": "ipaws", "enabled": True,
                                "target": "https://alerts.globaleas.org/IPAWSOPEN_EAS_SERVICE/rest/feed"})
                cfg.cfg_version = 4
            cfg.sources = [x for x in cfg.sources if x.get("kind") == "ipaws"]   # Lite: CAP feeds only
            cfg.watch_fips = []                  # no area filter any more (old configs may still hold one)
        except Exception:
            cfg = cls()
        cfg.cgen_style = STYLE_ALIASES.get(cfg.cgen_style, cfg.cgen_style)
        if not any(x.get("kind") == "ipaws" for x in cfg.sources):
            cfg.sources.insert(0, {"name": "IPAWS CAP feed", "kind": "ipaws",
                                   "enabled": True, "target": cfg.feed_url})
        return cfg

    def save(self, path=CONFIG_FILE):
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)
