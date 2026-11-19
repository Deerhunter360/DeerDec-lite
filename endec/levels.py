"""Level correction: quiet audio is brought up, loud audio is brought down, peaks are soft-limited."""
import numpy as np


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


def normalize(samples, sr, target_db=-16.0, max_gain_db=18.0):
    """Whole-clip correction (float or int16 in -> same kind out). Uses the typical level of the
    non-silent parts, so a loud tone or a pause does not skew it."""
    x = np.asarray(samples)
    is_int = x.dtype.kind == "i"
    f = x.astype(np.float64) / 32768.0 if is_int else x.astype(np.float64)
    if len(f) == 0:
        return x
    r = _frame_rms(f, sr)
    active = r[r > 0.004]
    if len(active) == 0:
        return x
    typical = float(np.median(active))
    gain = (10 ** (target_db / 20.0)) / typical
    gain = min(max(gain, 0.2), 10 ** (max_gain_db / 20.0))
    out = soft_limit(f * gain)
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
