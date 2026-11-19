"""Audio that comes with an IPAWS alert (the CAP 'EAS Broadcast Content' resource)."""
import array
import base64
import os
import subprocess
import tempfile
from typing import List, Optional

from .cap import _get

EXT = {"mpeg": ".mp3", "mp3": ".mp3", "wav": ".wav", "x-wav": ".wav", "ogg": ".ogg", "mp4": ".m4a",
       "aac": ".aac", "basic": ".au"}


def load_cap_audio(alert, sr: int, ffmpeg_path: str = "ffmpeg", log=print) -> Optional[List[float]]:
    """-> float samples at `sr`, or None when the alert carries no (usable) audio."""
    data = None
    try:
        if alert.audio_b64:
            data = base64.b64decode(alert.audio_b64)
        elif alert.audio_uri.lower().startswith("http"):
            data = _get(alert.audio_uri)
    except Exception as e:
        log(f"Could not get the alert's audio: {e}")
        return None
    if not data:
        return None
    try:
        from .monitor import find_ffmpeg
        exe = find_ffmpeg(ffmpeg_path)
    except Exception:
        exe = None
    if not exe:
        log("The alert has audio but ffmpeg was not found (pip install imageio-ffmpeg) - using a generated voice")
        return None
    ext = EXT.get(alert.audio_mime.lower().split("/")[-1], ".mp3")
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        r = subprocess.run([exe, "-nostdin", "-loglevel", "error", "-i", tmp, "-vn", "-ac", "1",
                            "-ar", str(sr), "-f", "s16le", "-"], capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"Could not decode the alert's audio: {e}")
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if r.returncode != 0 or len(r.stdout) < 2000:
        log("The alert's audio could not be decoded - using a generated voice")
        return None
    pcm = array.array("h")
    pcm.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    peak = max((abs(x) for x in pcm), default=1) or 1
    return [x / peak * 0.9 for x in pcm]
