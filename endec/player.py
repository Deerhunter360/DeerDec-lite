import shutil
import subprocess
import sys

from . import audio_out


def play_wav(path: str):
    # A chosen speaker goes through Qt; otherwise the plain system default is used.
    if audio_out.using_custom() and audio_out.play_wav_device(path):
        return
    if sys.platform.startswith("win"):
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    elif sys.platform == "darwin":
        subprocess.Popen(["afplay", path])
    else:
        for cmd in ("paplay", "aplay", "play"):
            if shutil.which(cmd):
                subprocess.Popen([cmd, path])
                return


def stop_audio():
    audio_out.stop_device()
    if sys.platform.startswith("win"):
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
