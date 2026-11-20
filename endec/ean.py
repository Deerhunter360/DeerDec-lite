"""The scripted National Emergency (EAN) sequence: intro, message after the tones, the repeating station
announcement while waiting for the EAT, and the closing announcement."""
import hashlib
import os
import threading
import wave

import numpy as np

from . import local

INTRO = "We interrupt our programming; this is a national emergency."

MESSAGE = ("This is an Emergency Action Notification requested by the White House. All EAS participants will follow "
           "activation procedures in the EAS Operating Handbook for a national level emergency. The President of the "
           "United States or his representative will shortly deliver a message over the Emergency Alert System.")

CONCLUDE = ("This concludes operations under the Emergency Alert System. All normal programming operations may now resume.")


def loop_text(cfg):
    st, area = local.station_name(cfg), local.area_name(cfg)
    return (f"This is an Emergency Action Notification. All broadcast stations and cable systems shall transmit this "
            f"Emergency Action Notification Message. This station has interrupted its regular programming at the request "
            f"of the White House to participate in the Emergency Alert System. During this emergency, most stations will "
            f"remain on the air providing news and information to the public in assigned areas. This is {st}. We will "
            f"continue to serve the {area} area. If you are not in this Local Area, you should tune to stations providing "
            f"news and information for your Local Area. You are listening to the Emergency Alert System serving the "
            f"counties of {area} area. Do not use your telephone. The telephone lines should be kept open for emergency "
            f"use. The Emergency Alert System has been activated for your local area.")


class Ean:
    def __init__(self, engine):
        self.engine = engine
        self._cache = {}
        self._lock = threading.Lock()

    def text(self, kind):
        return {"intro": INTRO, "message": MESSAGE, "loop": loop_text(self.engine.cfg), "conclude": CONCLUDE}[kind]

    def wav_text(self, text, name="attack"):
        """(path, seconds) of any text read aloud (used to repeat an attack warning), or (None, 0)."""
        e = self.engine
        base = int(e.encoder.sr)
        key = ("text:" + name, hashlib.md5(text.encode()).hexdigest())
        with self._lock:
            x = self._cache.get(key)
            if x is None:
                v = e.tts.synthesize(text, base, e.cfg.tts_rate, e.cfg.tts_voice, e.log)
                if not v:
                    return None, 0.0
                v = np.asarray(v, dtype=np.float64)
                if e.cfg.level_correct:
                    from .levels import normalize
                    v = normalize(v, base, e.cfg.level_target_db, e.cfg.level_max_gain_db)
                x = (np.clip(v, -1, 1) * 0.8 * 32767).astype("<i2")
                self._cache = {k: val for k, val in self._cache.items() if k[0] != key[0]}
                self._cache[key] = x
        pad = np.zeros(int(0.4 * base), "<i2")
        y = np.concatenate((pad, x, pad))
        os.makedirs(e.cfg.out_dir, exist_ok=True)
        path = os.path.join(e.cfg.out_dir, f"ean_{name}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(base)
            w.writeframes(y.tobytes())
        return path, len(y) / base

    def pcm(self, kind, sr=None):
        """int16 samples of that announcement (voice level-matched), or None if the voice engine is unavailable."""
        e = self.engine
        base = int(e.encoder.sr)
        text = self.text(kind)
        key = (kind, hashlib.md5(f"{text}|{e.cfg.tts_voice}|{e.cfg.tts_rate}".encode()).hexdigest())
        with self._lock:
            x = self._cache.get(key)
            if x is None:
                v = e.tts.synthesize(text, base, e.cfg.tts_rate, e.cfg.tts_voice, e.log)
                if not v:
                    return None
                v = np.asarray(v, dtype=np.float64)
                if e.cfg.level_correct:
                    from .levels import normalize
                    v = normalize(v, base, e.cfg.level_target_db, e.cfg.level_max_gain_db)
                x = (np.clip(v, -1, 1) * 0.8 * 32767).astype("<i2")
                self._cache = {k: val for k, val in self._cache.items() if k[0] != kind}      # drop stale text
                self._cache[key] = x
        if sr and int(sr) != base:
            n = int(len(x) * int(sr) / base)
            x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x.astype(np.float64)).astype("<i2")
        return x

    def wav(self, kind):
        """(path, seconds) of a WAV of that announcement (with a little silence either side), or (None, 0)."""
        x = self.pcm(kind)
        if x is None:
            return None, 0.0
        sr = int(self.engine.encoder.sr)
        pad = np.zeros(int(0.4 * sr), "<i2")
        x = np.concatenate((pad, x, pad))
        os.makedirs(self.engine.cfg.out_dir, exist_ok=True)
        path = os.path.join(self.engine.cfg.out_dir, f"ean_{kind}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(x.tobytes())
        return path, len(x) / sr

    def with_conclusion(self, path):
        """The EAT's own audio followed by 'This concludes operations...' twice, as ONE wav (so nothing can cut in
        between and the station / audio switch see one continuous alert).  Returns (new path, seconds) or None."""
        try:
            with wave.open(path) as w:
                prm, raw = w.getparams(), w.readframes(w.getnframes())
            sr = prm.framerate
            c = self.pcm("conclude", sr)
            if c is None:
                return None
            gap1, gap2, tail = np.zeros(int(1.5 * sr), "<i2"), np.zeros(int(3.0 * sr), "<i2"), np.zeros(int(1.0 * sr), "<i2")
            x = np.concatenate((np.frombuffer(raw, "<i2"), gap1, c, gap2, c, tail))
            out = os.path.join(os.path.dirname(path) or ".", "eat_" + os.path.basename(path))
            with wave.open(out, "wb") as w:
                w.setparams(prm)
                w.writeframes(x.tobytes())
            return out, len(x) / sr
        except Exception as e:
            self.engine.log(f"EAT: could not add the closing announcement: {e}")
            return None

    def warm(self):
        """Make the voices ahead of time so an activation never waits on the voice engine."""
        def go():
            for k in ("intro", "message", "loop", "conclude"):
                try:
                    self.pcm(k)
                except Exception as e:
                    self.engine.log(f"EAN script: could not prepare '{k}': {e}")
        threading.Thread(target=go, daemon=True).start()


ATTACK_SCRIPT = ("This is a Civil Danger Warning. Emergency officials have reported an attack, and your area is in danger. "
                 "This is not a drill. Take shelter immediately in the nearest sturdy building. If you can, go to a basement "
                 "or an interior room on the lowest floor, away from windows. Stay inside and do not travel. Do not use your "
                 "telephone except to report a life-threatening emergency, so the lines stay open. Keep your radio or "
                 "television tuned to this station for official instructions. Repeat: this is not a drill. Take shelter now.")
