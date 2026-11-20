"""Level correction: quiet audio is brought up, loud audio is brought down, peaks are soft-limited."""
import numpy as np

TONES = (853.0, 960.0, 1050.0, 1562.5, 2083.3)      # EAS two-tone, NWS tone, SAME data (mark / space)


def tone_rms(cfg):
    """RMS (0-1, before the final 0.8 scale) for generated headers and attention tones: the voice target plus a little."""
    if not getattr(cfg, "tone_level_match", True):
        return None
    return 10 ** ((float(cfg.level_target_db) + float(getattr(cfg, "tone_boost_db", 2.0))) / 20.0)


def tonal_frames(f, sr, ms=50):
    """Boolean per 50 ms frame: True where the frame is almost pure header / attention tone, not speech."""
    n = max(1, int(sr * ms / 1000))
    k = len(f) // n
    if k == 0:
        return np.zeros(0, bool)
    fr = f[:k * n].reshape(k, n) * np.hanning(n)
    sp = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    band = np.zeros(len(freqs), bool)
    for t in TONES:
        band |= np.abs(freqs - t) < 60
    tot = sp.sum(axis=1) + 1e-12
    pure = sp[:, band].sum(axis=1) / tot > 0.75                          # steady tones (attention signal)
    raw = f[:k * n].reshape(k, n)
    rms = np.sqrt(np.mean(raw ** 2, axis=1)) + 1e-12
    crest = np.max(np.abs(raw), axis=1) / rms
    fsk_band = (freqs > 1100) & (freqs < 2600)
    fsk = (sp[:, fsk_band].sum(axis=1) / tot > 0.85) & (crest < 1.7)     # SAME data bursts: constant level, 1.5-2.1 kHz
    return (pure | fsk) & (rms > 0.003)


def soft_limit(x):
    """Leave normal levels alone, squeeze anything above 0.7 smoothly so it never clips harshly."""
    a = np.abs(x)
    y = np.where(a <= 0.7, a, 0.7 + 0.28 * np.tanh((a - 0.7) / 0.28))
    return np.sign(x) * y


def _frame_rms(x, sr, ms=50):
    n = max(1, int(sr * ms / 1000))
    k = len(x) // n
    if k == 0:
        return np.array([float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0])
    return np.sqrt(np.mean(x[:k * n].reshape(k, n) ** 2, axis=1))


def normalize(samples, sr, target_db=-16.0, max_gain_db=18.0, tone_boost_db=None):
    """Whole-clip correction (float or int16 in -> same kind out). Uses the typical level of the
    non-silent speech, so a loud tone or a pause does not skew it. With tone_boost_db set, header and
    attention-tone frames are brought to the speech level plus that many dB (they are usually far louder)."""
    x = np.asarray(samples)
    is_int = x.dtype.kind == "i"
    f = x.astype(np.float64) / 32768.0 if is_int else x.astype(np.float64)
    if len(f) == 0:
        return x
    r = _frame_rms(f, sr)
    ton = tonal_frames(f, sr) if tone_boost_db is not None else np.zeros(len(r), bool)
    if len(ton) != len(r):
        ton = np.zeros(len(r), bool)
    speech = r[(r > 0.004) & ~ton]
    active = speech if len(speech) else r[r > 0.004]
    if len(active) == 0:
        return x
    typical = float(np.median(active))
    target = 10 ** (target_db / 20.0)
    gain = target / typical
    gain = min(max(gain, 0.2), 10 ** (max_gain_db / 20.0))
    out = f * gain
    if tone_boost_db is not None and ton.any():
        n = max(1, int(sr * 0.05))
        want = target * 10 ** (tone_boost_db / 20.0)
        g = np.ones(len(r))
        rr = _frame_rms(out, sr)
        g[ton] = want / np.maximum(rr[ton], 1e-6)           # tone frames: set to the wanted level
        centers = (np.arange(len(r)) + 0.5) * n
        out = out * np.interp(np.arange(len(out)), centers, g)
    out = soft_limit(out)
    return (np.clip(out, -1, 1) * 32767).astype("<i2") if is_int else out


class LevelAGC:
    """Live correction: follows the level chunk by chunk (fast down, slow up, holds through silence)."""

    def __init__(self, target_db=-16.0, max_gain_db=18.0):
        self.target = 10 ** (target_db / 20.0)
        self.max_gain = 10 ** (max_gain_db / 20.0)
        self.gain = 1.0

    def process(self, data: bytes) -> bytes:
        x = np.frombuffer(data[: len(data) // 2 * 2], "<i2").astype(np.float64) / 32768.0
        if len(x) == 0:
            return data
        rms = float(np.sqrt(np.mean(x ** 2)))
        if rms > 0.004:                                    # silence/noise floor: keep the current gain
            want = min(max(self.target / rms, 0.2), self.max_gain)
            self.gain += (want - self.gain) * (0.6 if want < self.gain else 0.06)
        out = soft_limit(x * self.gain)
        return (np.clip(out, -1, 1) * 32767).astype("<i2").tobytes()
