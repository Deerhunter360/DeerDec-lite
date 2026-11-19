"""Audio monitoring: SAME demodulator + stream/device runners.

  SameDemod      numpy AFSK (520.83 baud, 2083.3/1562.5 Hz) -> raw 'ZCZC-...' / 'NNNN' strings
  HeaderVoter    3-burst voting (2-of-3 match, or per-character majority)
  StreamRunner   ffmpeg -> 16-bit PCM -> decoder (Icecast / Shoutcast / HLS URLs)
  DeviceRunner   sounddevice input -> decoder
  MonitorManager keeps one runner per enabled audio source in Config.sources
"""
import collections
import queue
import subprocess
import threading
import time

import numpy as np

MARK, SPACE, BAUD = 2083.3333, 1562.5, 520.8333


# ============================================================ demodulator
class SameDemod:
    def __init__(self, sr, on_message):
        self.sr, self.on_message = sr, on_message
        self.T = sr / BAUD
        self.W = max(8, int(round(self.T)))
        self.n = 0                                   # global index of next input sample
        self.tail = {k: np.zeros(self.W - 1) for k in ("mc", "ms", "sc", "ss", "xx")}
        self.prev_h, self.prev_v = False, False
        self.phi = self.nc = self.last_t = None      # bit-clock PLL
        self.sr8, self.state, self.bitcnt, self.buf = 0, 0, 0, []

    # ---- DSP
    def feed(self, pcm):
        arr = np.asarray(pcm)
        x = arr.astype(np.float64) / (32768.0 if np.issubdtype(arr.dtype, np.integer) else 1.0)
        m = len(x)
        if m == 0:
            return
        idx = np.arange(self.n, self.n + m)
        pm = (2 * np.pi * MARK / self.sr * idx) % (2 * np.pi)
        ps = (2 * np.pi * SPACE / self.sr * idx) % (2 * np.pi)
        prods = {"mc": x * np.cos(pm), "ms": x * np.sin(pm),
                 "sc": x * np.cos(ps), "ss": x * np.sin(ps), "xx": x * x}
        box, out = np.ones(self.W), {}
        for k, v in prods.items():
            ext = np.concatenate((self.tail[k], v))
            out[k] = np.convolve(ext, box, mode="valid")
            self.tail[k] = ext[-(self.W - 1):]
        em = out["mc"] ** 2 + out["ms"] ** 2
        es = out["sc"] ** 2 + out["ss"] ** 2
        ex = out["xx"]
        valid = ((em + es) > 0.12 * self.W * ex) & (ex > self.W * 1e-6)   # "is this AFSK?" gate
        h = em > es
        self._clock(h, valid, m)
        self.prev_h, self.prev_v = bool(h[-1]), bool(valid[-1])
        self.n += m

    def _emit_upto(self, limit, h, valid, m):
        while self.nc is not None and self.nc < limit:
            ci = int(round(self.nc)) - self.n
            if 0 <= ci < m:
                if valid[ci]:
                    self._bit(bool(h[ci]))
                else:
                    self._reset_bytes()
            self.nc += self.T

    def _clock(self, h, valid, m):
        eh = np.concatenate(([self.prev_h], h))
        ev = np.concatenate(([self.prev_v], valid))
        trans = np.flatnonzero(ev[1:] & ev[:-1] & (eh[1:] != eh[:-1]))
        T = self.T
        for i in trans:
            t = self.n + int(i)
            self._emit_upto(t, h, valid, m)
            if self.phi is None or self.last_t is None or t - self.last_t > 20 * T:
                self._reset_bytes()
                self.phi, self.nc = float(t), t + T / 2
            else:
                err = ((t - self.phi + T / 2) % T) - T / 2
                self.phi += 0.25 * err
                base = self.phi + T / 2
                self.nc = base + round((self.nc - base) / T) * T
            self.last_t = t
        self._emit_upto(self.n + m, h, valid, m)

    # ---- bits -> SAME text
    def _reset_bytes(self):
        self.state, self.bitcnt, self.buf = 0, 0, []

    def _bit(self, b):
        self.sr8 = ((self.sr8 >> 1) | (0x80 if b else 0)) & 0xFF
        if self.state == 0:
            if self.sr8 == 0xAB:
                self.state, self.bitcnt = 1, 0
            return
        self.bitcnt += 1
        if self.bitcnt < 8:
            return
        self.bitcnt, byte = 0, self.sr8
        if self.state == 1:
            if byte == 0xAB:
                return
            if byte in (0x5A, 0x4E):                       # 'Z' / 'N'
                self.buf, self.state = [byte], 2
            else:
                self.state = 0
            return
        if not 0x20 <= byte <= 0x7E:
            self._reset_bytes()
            return
        self.buf.append(byte)
        s = bytes(self.buf)
        if len(s) == 4 and s not in (b"ZCZC", b"NNNN"):
            self._reset_bytes()
        elif s == b"NNNN":
            self._reset_bytes()
            self.on_message("NNNN")
        elif len(s) > 268:
            self._reset_bytes()
        elif byte == 0x2D and s.startswith(b"ZCZC"):
            p = s.find(b"+")
            if p >= 0 and s[p:].count(b"-") >= 3:
                self._reset_bytes()
                self.on_message(s.decode("ascii"))


# ============================================================ burst voting
class HeaderVoter:
    """Headers are sent 3x. Confirm on 2 identical bursts, else per-character vote."""
    WINDOW = 10.0

    def __init__(self, on_confirm):
        self.on_confirm = on_confirm
        self.msgs, self.start, self.done = [], 0.0, False

    def add(self, raw, now=None):
        now = time.time() if now is None else now
        if raw == "NNNN":
            return
        if now - self.start > self.WINDOW or not self.msgs:
            self.msgs, self.start, self.done = [], now, False
        if self.done:
            return
        self.msgs.append(raw)
        if self.msgs.count(raw) >= 2:
            self._confirm(raw)
        elif len(self.msgs) >= 3:
            self._confirm(self._vote())

    def tick(self, now=None):
        now = time.time() if now is None else now
        if self.msgs and not self.done and now - self.start > 7.0:
            self._confirm(self._vote())

    def _vote(self):
        same_len = [m for m in self.msgs if len(m) == len(self.msgs[0])]
        if len(same_len) >= 3:
            return "".join(collections.Counter(c).most_common(1)[0][0] for c in zip(*same_len))
        return collections.Counter(self.msgs).most_common(1)[0][0]

    def _confirm(self, raw):
        self.done = True
        self.on_confirm(raw)


def decode_wav(path):
    """Run the SAME decoder over a WAV file (8/16-bit PCM, any rate/channels).
    -> (all bursts heard, confirmed headers)."""
    import wave
    with wave.open(path) as w:
        ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        a = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
    elif width == 1:
        a = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128.0) / 128.0
    else:
        raise ValueError("Only 8-bit or 16-bit PCM WAV files are supported")
    if ch > 1:
        a = a[: len(a) // ch * ch].reshape(-1, ch).mean(axis=1)
    bursts, confirmed = [], []
    voter = HeaderVoter(confirmed.append)
    demod = SameDemod(sr, lambda m: (bursts.append(m), voter.add(m, now=demod.n / sr)))
    for i in range(0, len(a), 4096):
        demod.feed(a[i:i + 4096])
    voter.tick(now=demod.n / sr + 10)
    return bursts, confirmed


def count_header_bursts(pcm, sr):
    """How many SAME header bursts (not EOM) are really in this audio."""
    found = []
    demod = SameDemod(int(sr), lambda m: found.append(m) if m != "NNNN" else None)
    a = np.asarray(pcm, np.float64) / 32768.0
    for i in range(0, len(a), 4096):
        demod.feed(a[i:i + 4096])
    return len(found)


def _tone_ratio(seg, sr, win=None):
    """(853+960 share, 1050 share) of the speech-band power in this ~0.2 s segment."""
    n = len(seg)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    sp = np.abs(np.fft.rfft(seg * (np.hanning(n) if win is None else win))) ** 2
    tot = sp[(freqs > 300) & (freqs < 3400)].sum() + 1e-12     # ignore hum below and hiss above
    band = lambda f, w=30: sp[(freqs > f - w) & (freqs < f + w)].sum()
    b853, b960, b1050 = band(853), band(960), band(1050, 40)   # NWR tones drift a little, so 1050 gets more room
    two = b853 > 0.1 * tot and b960 > 0.1 * tot and (b853 + b960) > 0.5 * tot
    one = b1050 > 0.38 * tot
    return two, one


def _norm(x):
    x = np.asarray(x, dtype=np.float64)
    return x / 32768.0 if (x.dtype.kind == "i" or np.abs(x).max(initial=0) > 2) else x


def tone_hit(seg, sr):
    """True if this short segment (about 0.2 s) is an EAS attention signal (853+960 Hz or NWR 1050 Hz)."""
    seg = _norm(seg)
    if len(seg) < 64 or np.sqrt(np.mean(seg ** 2)) < 0.004:
        return False
    two, one = _tone_ratio(seg, sr)
    return bool(two or one)


def tone_kind(x, sr):
    """Which attention signal is in this audio: '853 + 960 Hz' or '1050 Hz'."""
    x = _norm(x)
    if len(x) < 64:
        return "attention signal"
    two, one = _tone_ratio(x, sr)
    return "1050 Hz" if (one and not two) else "853 + 960 Hz"


def tone_onset(x, sr, need=3):
    """Sample index where a sustained attention signal (>= need x 0.2 s) begins, or None."""
    x = _norm(x)
    n = int(0.2 * sr)
    win = np.hanning(n)
    hits, start = 0, 0
    for i in range(0, len(x) - n + 1, n // 2):
        seg = x[i:i + n]
        ok = False
        if np.sqrt(np.mean(seg ** 2)) >= 0.004:
            two, one = _tone_ratio(seg, sr, win)
            ok = two or one
        if ok:
            if hits == 0:
                start = i
            hits += 1
            if hits >= need * 2 - 1:                   # half-overlapping windows
                return start
        else:
            hits = 0
    return None


def tone_present(x, sr):
    """True if int16/float samples contain an EAS attention signal for at least ~0.6 s."""
    return tone_onset(x, sr) is not None


def make_attn(sr, seconds, org=""):
    """Synthetic attention signal: 1050 Hz for the NWS (WXR), else the 853+960 Hz EAS two-tone."""
    t = np.arange(int(sr * seconds)) / sr
    if org == "WXR":
        y = 0.5 * np.sin(2 * np.pi * 1050 * t)
    else:
        y = 0.25 * (np.sin(2 * np.pi * 853 * t) + np.sin(2 * np.pi * 960 * t))
    f = int(0.02 * sr)
    y[:f] *= np.linspace(0, 1, f)
    y[-f:] *= np.linspace(1, 0, f)
    return (y * 32767).astype("<i2")


def make_eom(sr, lead=0.5):
    """Three NNNN bursts, one second apart, preceded by `lead` seconds of silence."""
    from .same import AfskEncoder
    enc = AfskEncoder(int(sr))
    burst = enc._afsk(b"\xAB" * 16 + b"NNNN")
    s = [0.0] * int(sr * lead)
    for _ in range(3):
        s += burst + [0.0] * int(sr)
    return (np.clip(np.array(s), -1, 1) * 0.8 * 32767).astype("<i2")


def make_header_block(sr, raw, seconds, org=""):
    """Generated 'start of alert': the SAME header x3, then the attention tone."""
    from .same import AfskEncoder
    enc = AfskEncoder(int(sr))
    burst = enc._afsk(b"\xAB" * 16 + raw.encode("ascii", "ignore"))
    s = [0.0] * int(sr * 0.3)
    for _ in range(3):
        s += burst + [0.0] * int(sr)
    head = (np.clip(np.array(s), -1, 1) * 0.8 * 32767).astype("<i2")
    return np.concatenate((head, make_attn(sr, seconds, org), np.zeros(int(0.5 * sr), "<i2")))


# ============================================================ runners
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_ffmpeg(configured="ffmpeg"):
    """Configured path / PATH -> common Windows folders -> imageio-ffmpeg's bundled binary."""
    import os
    import shutil
    hit = shutil.which(configured)
    if hit:
        return hit
    for p in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe")):
        if os.path.exists(p):
            return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


class _Runner(threading.Thread):
    kind = ""

    def __init__(self, src, manager):
        super().__init__(daemon=True)
        self.name_, self.target = src["name"], src["target"]
        self.manager = manager
        self.stop_evt = threading.Event()
        self.voter = HeaderVoter(self._confirmed)
        # recording state (see _tap)
        self._gn, self._cur_len, self._sr = 0, 0, 22050
        self._ring, self._ring_n = collections.deque(), 0
        self._rec, self._rec_n, self._rec_base = None, 0, 0
        self._eoms, self._eom_t = 0, None
        self._hdr_end, self._attn_done, self._live, self._raw_last = None, True, False, ""
        self._burst_s, self._injected = 2.0, False
        self._eom_gn, self._eom_seen = None, False
        # live relay: silent through header + attention tone, then plays
        self._ls, self._live_open, self._live_ever = "", False, False
        self._hold, self._hold_n, self._tpos, self._a0, self._miss, self._tend = collections.deque(), 0, 0, 0, 0, 0

    def stop(self):
        self.stop_evt.set()

    def _stop_set(self):
        return self.stop_evt.is_set()

    def run(self):
        try:
            self.work()
        except Exception as e:                       # the manager restarts a runner that died
            self._set(state=f"Crashed: {str(e)[:60]}", level=0.0)
            self.manager.engine.log(f"Audio [{self.name_}] crashed: {e}")

    def _set(self, **kw):
        self.manager.status.setdefault(self.name_, {}).update(kw)

    def _confirmed(self, raw):
        self._raw_last = raw
        self._set(last=raw)
        self.manager.on_header(raw, self.name_)

    def _burst(self, raw):
        st = self.manager.status.setdefault(self.name_, {})
        st["bursts"] = st.get("bursts", 0) + 1
        self.manager.engine.log(f"Audio [{self.name_}] burst {st['bursts']}: {raw}")
        if raw == "NNNN":
            self._eom()
        else:
            self._begin()
            self._rec_bursts = getattr(self, "_rec_bursts", 0) + 1
            self._hdr_end = self._gn + self._cur_len       # about where this header burst ended
            self._burst_s = (16 + len(raw)) * 8 / BAUD     # how long one header burst lasts
            self._attn_done = False
            self.voter.add(raw)

    def _decoder(self, sr):
        return SameDemod(sr, self._burst)

    def _feed(self, dec, pcm):
        self._cur_len = len(pcm)
        dec.feed(pcm)

    def _level(self, pcm):
        peak = float(np.abs(pcm).max(initial=0)) / 32768.0
        old = self.manager.status.get(self.name_, {}).get("level", 0.0)
        self._set(level=max(peak, old * 0.85))

    # ---- recording of the audio that follows a header (EASyPLUS-style capture)
    PRE_SECONDS = 8.0          # audio kept from before the first burst finished (covers the burst itself)
    EOM_TAIL = 6.0

    def _org(self):
        p = self._raw_last.split("-")
        return p[1] if len(p) > 1 else ""

    def _tap(self, pcm, sr):
        """Called with every chunk of PCM (int16 array) this source produces."""
        data = pcm.astype("<i2").tobytes()
        self._sr = sr
        after = self._gn + len(pcm)
        if self._rec is not None:
            self._rec.append(data)
            self._rec_n += len(data)
            over = self._rec_n / 2 / sr > self.manager.cfg.relay_max_seconds
            if over or (self._eom_t is not None and time.time() >= self._eom_t):
                self._gn = after
                self._finish("max length" if over else "EOM")
                return
            if self._live and not self._live_open:
                self._live_guard(after, sr)
        else:
            self._ring.append(data)
            self._ring_n += len(data)
            while self._ring_n > self.PRE_SECONDS * sr * 2 and len(self._ring) > 1:
                self._ring_n -= len(self._ring.popleft())
        self._gn = after

    def _begin(self):
        if self.manager.cfg.audio_relay == "off" or self._rec is not None:
            return
        self._rec = list(self._ring)
        self._rec_n = sum(len(b) for b in self._rec)
        self._rec_base = self._gn - self._rec_n // 2
        self._eoms, self._eom_t = 0, None
        self._injected, self._eom_gn, self._eom_seen = False, None, False
        self._hdr_end = None
        self._rec_bursts = 0
        self.manager.engine.log(f"Recording audio from {self.name_}…")

    HOLD_S = 0.6               # live audio is held back this long so a source EOM can be dropped

    def _xbuf(self):
        return np.frombuffer(b"".join(self._rec), "<i2")

    def _live_guard(self, after, sr):
        """Never let a hiccup in tone handling leave the live alert silent: log it and just start playing."""
        try:
            self._live_step(after, sr)
            if self._ls == "wait_header" and self._hdr_end is not None and \
                    (after - self._hdr_end) > 15 * sr:                  # stuck waiting for the header to finish
                raise RuntimeError("still waiting 15 s after the header")
        except Exception as e:
            self.manager.engine.log(f"Live relay problem ({e}) - starting live audio without waiting for the tone")
            try:
                if self._ls != "open":
                    self._gen_start(sr)
                    self._open_live(self._xbuf()[max(0, self._hdr_end - self._rec_base + int(1.0 * sr)):], sr)
            except Exception as e2:
                self.manager.engine.log(f"Live relay could not start: {e2}")

    def _live_step(self, after, sr):
        """Silent until the attention tone is over, then start relaying."""
        log = self.manager.engine.log
        if self._hdr_end is None:
            return
        base = self._rec_base
        if self._ls == "wait_header":
            if after < self._hdr_end + int((1.0 + self._burst_s + 0.6) * sr):
                return                                  # header bursts still coming
            x = self._xbuf()
            h0 = max(0, self._hdr_end - base + int(0.2 * sr))
            on = tone_onset(x[h0:h0 + int(5.0 * sr)], sr)            # look up to 5 s after the header for it
            if on is not None:
                self._a0 = h0 + on
                kind = tone_kind(x[self._a0:self._a0 + int(1.0 * sr)], sr)
                log(f"Attention tone detected on {self.name_} ({kind}) - sending DeerDec's own header + attention tone, "
                    f"live audio follows when the source tone ends")
                self._gen_start(sr)
                self._ls, self._tpos, self._miss = "in_tone", self._a0, 0
            else:
                log(f"No attention tone heard on {self.name_} - sending DeerDec's own header + attention tone, then live audio")
                self._gen_start(sr)
                self._a0 = h0 + int(1.0 * sr)
                self._open_live(x[self._a0:], sr)
        elif self._ls == "in_tone":
            win = int(0.2 * sr)
            x = None
            while (after - base) >= self._tpos + win:
                x = self._xbuf() if x is None else x
                if tone_hit(x[self._tpos:self._tpos + win], sr):
                    self._miss = 0
                else:
                    if self._miss == 0:
                        self._tend = self._tpos
                    self._miss += 1
                self._tpos += win
                long = (self._tpos - self._a0) / sr > 45
                if self._miss >= 2 or long:
                    secs = (self._tend if self._miss >= 2 else self._tpos) - self._a0
                    log(f"Attention tone ended after {secs / sr:.1f}s - live audio started")
                    self._open_live(x[(self._tend if self._miss >= 2 else self._tpos):], sr)
                    return

    def _gen_start(self, sr):
        """Live: play generated headers (3x) + attention tone first; the live audio queues up behind them."""
        try:
            secs = max(3, self.manager.cfg.attn_seconds)
            blk = make_header_block(sr, self._raw_last, secs, self._org()) if self._raw_last else \
                np.concatenate((make_attn(sr, secs, self._org()), np.zeros(int(0.5 * sr), "<i2")))
            self.manager.inject(sr, blk.tobytes())
        except Exception as e:
            self.manager.engine.log(f"Live: could not generate header/tone: {e}")

    def _open_live(self, tail, sr):
        from .levels import LevelAGC
        c = self.manager.cfg
        self._agc = LevelAGC(c.level_target_db, c.level_max_gain_db) if c.level_correct else None
        self._live_open = self._live_ever = True
        self._ls = "open"
        self._hold.clear()
        self._hold_n = 0
        self._push_live(np.asarray(tail, "<i2").tobytes(), sr)

    def _push_live(self, data, sr):
        """Relay with a small hold-back (so the source's own EOM can still be cut off)."""
        if getattr(self, "_agc", None):
            data = self._agc.process(data)
        self._hold.append(data)
        self._hold_n += len(data)
        keep = int(self.HOLD_S * sr) * 2
        while self._hold_n > keep:
            d, extra = self._hold[0], self._hold_n - keep
            if len(d) <= extra:
                self._hold.popleft()
                self._hold_n -= len(d)
                self.manager.push_audio(self.name_, d, sr)
            else:
                n = extra // 2 * 2
                self.manager.push_audio(self.name_, d[:n], sr)
                self._hold[0] = d[n:]
                self._hold_n -= n

    def _out(self, data, sr):
        """Where this source's audio goes: during a live relay only through the gate above."""
        if self._live and self.manager.relay_source == self.name_:
            if self._live_open:
                self._push_live(data, sr)
            return
        if self._rec is not None and not self._live and self.manager.cfg.audio_relay == "live":
            return                                      # live mode: a header was heard, keep the source silent until DeerDec decides
        self.manager.push_audio(self.name_, data, sr)

    def live_start(self):
        """Called once the alert is accepted and live relay is chosen."""
        if self._rec is not None:
            self._live, self._live_open, self._live_ever, self._ls = True, False, False, "wait_header"
            self._hold.clear()
            self._hold_n = 0
            self.manager.relay_begin(self.name_, self._sr, b"")      # nothing plays yet

    def _eom(self):
        if self._rec is None:
            return
        self._eoms += 1
        if self._eom_gn is None:                      # the source's own EOM: remember where it began
            self._eom_gn = self._gn + self._cur_len
            self._eom_seen = True                     # stop relaying the source; we send our own EOM
            self._hold.clear()                        # drop the held-back tail, which contains this EOM burst
            self._hold_n = 0
            self._live_open = False
        if self._eoms >= 3:
            self._eom_t = time.time() + 0.6
        elif self._eom_t is None:
            self._eom_t = time.time() + self.EOM_TAIL

    def _finish(self, why):
        import os
        import wave
        live_was = self._live
        chunks, self._rec, self._eom_t, self._live = self._rec, None, None, False
        self._ring.clear()
        self._ring_n = 0
        pcm = np.frombuffer(b"".join(chunks), "<i2")
        sr, cfg, log = int(self._sr), self.manager.cfg, self.manager.engine.log
        # cut where the source's own EOM began; DeerDec sends a clean EOM itself at the end
        if self._eom_gn is not None:
            cut = self._eom_gn - self._rec_base - int(0.40 * sr)
            if 0 < cut < len(pcm):
                pcm = pcm[:cut]
        eom = make_eom(sr)
        nb = 0
        if self._hdr_end is not None and not live_was:
            hh = max(0, self._hdr_end - self._rec_base)
            try:
                nb = count_header_bursts(pcm[:hh + int(1.5 * sr)], sr)
            except Exception:
                nb = 0
            log(f"Recording from {self.name_} contains {nb} header burst(s)")
        if 0 < nb < 3 and self._raw_last and not live_was:     # a burst was missed: put the missing ones back
            from .same import AfskEncoder
            miss = 3 - nb
            b = AfskEncoder(sr)._afsk(b"\xAB" * 16 + self._raw_last.encode("ascii", "ignore"))
            one = (np.clip(np.array(b + [0.0] * int(sr)), -1, 1) * 0.8 * 32767).astype("<i2")
            pcm = np.concatenate((np.tile(one, miss), pcm))
            if self._hdr_end is not None:
                self._hdr_end += len(one) * miss
                self._rec_base -= 0
            log(f"Only {nb} header burst(s) were heard - added {miss} generated so the alert has 3")
        if cfg.level_correct and len(pcm):
            from .levels import normalize
            pcm = normalize(pcm, sr, cfg.level_target_db, cfg.level_max_gain_db)
        # a source that drops the attention tone gets a generated one, right after the header
        if self._hdr_end is not None:
            h = self._hdr_end - self._rec_base
            if not tone_present(pcm[h + int(0.5 * sr): h + int(40 * sr)], sr):
                secs = max(3, cfg.attn_seconds)
                ins = min(len(pcm), h + int(1.0 * sr))
                pcm = np.concatenate((pcm[:ins], make_attn(sr, secs, self._org()),
                                      np.zeros(int(0.5 * sr), "<i2"), pcm[ins:]))
                log(f"No attention tone in the recording from {self.name_} - generated one ({secs}s)")
        if live_was and self._live_ever:              # live: the EOM plays after whatever is still queued
            self.manager.inject(sr, eom.tobytes())
        self._live_open = self._live_ever = False
        self._hold.clear()
        self._hold_n = 0
        pcm = np.concatenate((pcm, eom))
        secs = len(pcm) / sr
        os.makedirs(cfg.out_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in self.name_)
        path = os.path.join(cfg.out_dir, f"heard_{int(time.time())}_{safe}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.astype("<i2").tobytes())
        log(f"Recorded {secs:.0f}s from {self.name_} ({why}) -> {path}")
        self.manager.relay_end(self.name_, path, secs)


class StreamRunner(_Runner):
    kind = "stream"
    SR = 22050
    STALL_S, CONNECT_S = 25, 40          # no data for this long -> drop and reconnect

    def work(self):
        backoff = 2
        while not self.stop_evt.is_set():
            http = self.target.lower().startswith(("http://", "https://"))
            exe = find_ffmpeg(self.manager.cfg.ffmpeg_path)
            if not exe:
                self._set(state="ffmpeg not found - run: pip install imageio-ffmpeg", level=0.0)
                self.stop_evt.wait(15)
                continue
            cmd = [exe, "-nostdin", "-loglevel", "error"] + (
                ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"] if http else []
            ) + ["-i", self.target, "-vn", "-ac", "1", "-ar", str(self.SR), "-f", "s16le", "-"]
            try:
                self._set(state="Connecting…")
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=CREATE_NO_WINDOW)
            except FileNotFoundError:
                self._set(state="ffmpeg not found (set path in Config)", level=0.0)
                self.stop_evt.wait(10)
                continue
            dec, got = self._decoder(self.SR), False
            st = {"last": time.time(), "got": False, "stalled": False}

            def watchdog(proc=proc, st=st):
                """ffmpeg can hang with the connection open but no audio: kill it so we reconnect."""
                while not self.stop_evt.wait(2):
                    if proc.poll() is not None:
                        return
                    if time.time() - st["last"] > (self.STALL_S if st["got"] else self.CONNECT_S):
                        st["stalled"] = True
                        proc.kill()
                        return
                proc.kill()
            threading.Thread(target=watchdog, daemon=True).start()
            while not self.stop_evt.is_set():
                data = proc.stdout.read(4096)
                if not data:
                    break
                st["last"] = time.time()
                if not got:
                    got, backoff = True, 2
                    st["got"] = True
                    self._set(state="Listening")
                pcm = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")
                self._feed(dec, pcm)
                self._level(pcm)
                self._tap(pcm, self.SR)
                self._out(pcm.tobytes(), self.SR)
                self.voter.tick()
            err = ""
            try:
                proc.kill()
                err = proc.stderr.read().decode(errors="ignore").strip().splitlines()[-1:]
                err = err[0][:80] if err else ""
            except Exception:
                pass
            if self.stop_evt.is_set():
                break
            if st["stalled"]:
                err = "stream stalled"
                self.manager.engine.log(f"Audio [{self.name_}] stalled - reconnecting")
            self._set(state=f"Reconnecting in {backoff}s" + (f" ({err})" if err else ""), level=0.0)
            self.stop_evt.wait(backoff)
            backoff = min(backoff * 2, 30)


class DeviceRunner(_Runner):
    kind = "device"
    STALL_S = 10                         # no audio frames for this long -> reopen the device

    def work(self):
        try:
            import sounddevice as sd
        except ImportError:
            self._set(state="pip install sounddevice", level=0.0)
            return
        while not self.stop_evt.is_set():
            idx, info = None, None
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and (d["name"] == self.target or self.target in d["name"]
                                                    or d["name"] in self.target):
                    idx, info = i, d
                    break
            if idx is None:
                self._set(state="Device not found", level=0.0)
                self.stop_evt.wait(5)
                continue
            sr = int(info["default_samplerate"])
            q = queue.Queue()
            try:
                with sd.InputStream(device=idx, channels=1, samplerate=sr, dtype="int16",
                                    callback=lambda indata, f, t, s: q.put(indata[:, 0].copy())):
                    self._set(state="Listening")
                    dec = self._decoder(sr)
                    last = time.time()
                    while not self.stop_evt.is_set():
                        try:
                            pcm = q.get(timeout=1)
                            last = time.time()
                        except queue.Empty:
                            if time.time() - last > self.STALL_S:
                                raise RuntimeError("no audio from the device - reopening")
                            continue
                        self._feed(dec, pcm)
                        self._level(pcm)
                        self._tap(pcm, sr)
                        self._out(pcm.astype("<i2").tobytes(), sr)
                        self.voter.tick()
            except Exception as e:
                self._set(state=f"Error: {str(e)[:60]} (retrying)", level=0.0)
                self.manager.engine.log(f"Audio [{self.name_}]: {e}")
                self.stop_evt.wait(5)


# ============================================================ manager
class MonitorManager:
    """One runner per enabled stream/device in cfg.sources; edits apply within ~2 s."""

    def __init__(self, engine):
        self.engine, self.cfg = engine, engine.cfg
        self.status, self.runners = {}, {}
        self.listen_name = None                       # source whose audio is played aloud (None = silent)
        self.listen_q = queue.Queue(maxsize=300)      # (sample_rate, pcm bytes) for the GUI player
        self.relay_source = None                      # source being relayed live right now
        self.flush_gen = 0
        self.busy_cb = None                           # GUI: True while an alert is on screen / playing
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def push_audio(self, name, data, sr):
        r = self.runners.get(name)
        if r is not None and name == self.relay_source and r._eom_seen:
            return                                    # live relay: source EOM reached, we send our own
        if name == self.listen_name:
            try:
                self.listen_q.put_nowait((sr, data))
            except queue.Full:
                pass

    def decide_mode(self, source):
        """off / record / live for the alert just confirmed on `source`."""
        r = self.runners.get(source)
        if r is None or r._rec is None or self.cfg.audio_relay == "off":
            return "off"
        if self.cfg.audio_relay == "live" and not (self.busy_cb and self.busy_cb()):
            return "live"
        return "record"

    def accepted(self, source, mode):
        r = self.runners.get(source)
        if mode == "live" and r:
            r.live_start()
        elif mode == "record":
            self.engine.log(f"Alert heard on {source}: recording, will play when it ends")

    def inject(self, sr, data):
        try:
            self.listen_q.put_nowait((sr, data))
        except queue.Full:
            pass

    def relay_begin(self, name, sr, backlog):
        """Live mode: play this source aloud until the alert ends."""
        self._prev_listen, self.relay_source = self.listen_name, name
        self.set_listen(name)
        try:
            self.listen_q.put_nowait((sr, backlog))
        except queue.Full:
            pass

    def relay_end(self, name, path, secs):
        if self.relay_source == name:                 # live: back to what it was; queued audio still plays out
            self.relay_source = None
            self.listen_name = getattr(self, "_prev_listen", None)
        self.engine.relay_done(name, path, secs)

    def set_listen(self, name):
        self.listen_name = name
        self.flush_gen += 1                           # tells the player to drop anything still buffered
        while not self.listen_q.empty():
            try:
                self.listen_q.get_nowait()
            except queue.Empty:
                break

    def on_header(self, raw, source):
        """Runs off the audio thread: building the alert takes a moment and must not drop incoming audio."""
        def go():
            try:
                self.engine.log(f"Audio [{source}]: {raw}")
                self.engine.process_decoded(raw, source)
            except Exception as e:
                self.engine.log(f"Alert handling failed: {e}")
        threading.Thread(target=go, daemon=True).start()

    def abort(self):
        """Throw away whatever alert is being recorded/relayed: nothing more is played from it."""
        for r in list(self.runners.values()):
            r._rec, r._eom_t, r._live, r._live_open, r._live_ever, r._ls = None, None, False, False, False, ""
            r._hold.clear()
            r._hold_n = 0
            r._ring.clear()
            r._ring_n = 0
        if self.relay_source is not None:
            self.relay_source = None
            self.listen_name = getattr(self, "_prev_listen", None)
        self.flush_gen += 1                           # the listener drops anything still buffered
        while not self.listen_q.empty():
            try:
                self.listen_q.get_nowait()
            except queue.Empty:
                break
        self.engine.log("Alert aborted - recording/relay discarded")

    def restart_all(self):
        """Drop every connection; the loop below recreates them within ~2 s."""
        for r in list(self.runners.values()):
            r.stop()
        self.runners.clear()
        self.status.clear()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            wanted = {s["name"]: s for s in self.cfg.sources
                      if s["kind"] in ("stream", "device") and s.get("enabled", True)}
            for name, r in list(self.runners.items()):
                s = wanted.get(name)
                if s and not r.is_alive() and not r._stop_set():
                    self.engine.log(f"Audio [{name}] stopped unexpectedly - restarting")
                    del self.runners[name]
                    self.status.pop(name, None)
                    continue
                if not s or s["target"] != r.target or s["kind"] != r.kind:
                    r.stop()
                    del self.runners[name]
                    self.status.pop(name, None)
            for name, s in wanted.items():
                if name not in self.runners:
                    r = (StreamRunner if s["kind"] == "stream" else DeviceRunner)(s, self)
                    self.status[name] = {"state": "Starting…", "level": 0.0, "last": ""}
                    self.runners[name] = r
                    r.start()
            self._stop.wait(2)
        for r in self.runners.values():
            r.stop()
        self.runners.clear()
        self.status.clear()
