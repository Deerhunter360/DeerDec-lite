import array
import os
import subprocess
import sys
import tempfile
import wave
from typing import List, Optional

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_worker.py")


class Tts:
    """Offline text-to-speech via pyttsx3 (Windows SAPI5). Returns float samples
    at the encoder's sample rate, ready to drop between the tone and the EOM."""

    def synthesize(self, text: str, sr: int, rate: int = 165, voice: str = "",
                   log=print) -> Optional[List[float]]:
        if not text.strip():
            return None
        tmp = tempfile.mkdtemp()
        txt, out = os.path.join(tmp, "t.txt"), os.path.join(tmp, "v.wav")
        try:
            with open(txt, "w", encoding="utf-8") as f:
                f.write(text)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            r = subprocess.run([sys.executable, WORKER, txt, out, str(rate), voice],
                               capture_output=True, text=True, timeout=120, creationflags=flags)
            if r.returncode != 0 or not os.path.exists(out):
                log(f"TTS failed (pip install pyttsx3?): {r.stderr.strip()[-200:]}")
                return None
            return self._load(out, sr)
        except Exception as e:
            log(f"TTS error: {e}")
            return None

    @staticmethod
    def _load(path, sr):
        with wave.open(path) as w:
            ch, width, src_sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
        if width != 2:
            raise ValueError("unsupported TTS sample width")
        pcm = array.array("h", raw)
        mono = [pcm[i] / 32768.0 for i in range(0, len(pcm) - ch + 1, ch)]
        if src_sr != sr and mono:                       # linear resample
            ratio, m = src_sr / sr, int(len(mono) * sr / src_sr)
            res = []
            for i in range(m):
                pos = i * ratio
                j = int(pos)
                frac = pos - j
                nxt = mono[j + 1] if j + 1 < len(mono) else mono[j]
                res.append(mono[j] * (1 - frac) + nxt * frac)
            mono = res
        peak = max((abs(x) for x in mono), default=1) or 1
        return [x * 0.9 / peak for x in mono]
