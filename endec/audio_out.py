"""Choice of speaker/output device for everything DeerDec plays (alert audio, live relay, test beep)."""
import io
import math
import struct
import wave

DEFAULT_LABEL = "System default"
_choice = ""            # saved device description ("" = system default)
_sink = None
_buf = None


def set_output(name: str):
    global _choice
    _choice = name or ""


def list_outputs():
    """Descriptions of every output device Windows offers."""
    try:
        from PySide6.QtMultimedia import QMediaDevices
        return [d.description() for d in QMediaDevices.audioOutputs()]
    except Exception:
        return []


def pick_output(name=None):
    """QAudioDevice for the chosen name, or the system default if unset/missing."""
    from PySide6.QtMultimedia import QMediaDevices
    name = _choice if name is None else name
    if name:
        for d in QMediaDevices.audioOutputs():
            if d.description() == name:
                return d
    return QMediaDevices.defaultAudioOutput()


def using_custom() -> bool:
    return bool(_choice)


def play_wav_device(path: str, parent=None):
    """Play a WAV file on the chosen output with QtMultimedia. Returns False if it can't."""
    global _sink, _buf
    try:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink
        with wave.open(path, "rb") as w:
            ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            data = w.readframes(w.getnframes())
        if sw != 2:
            return False
        stop_device()
        fmt = QAudioFormat()
        fmt.setSampleRate(sr)
        fmt.setChannelCount(ch)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        dev = pick_output()
        if dev.isNull():
            return False
        _buf = QBuffer()
        _buf.setData(QByteArray(data))
        _buf.open(QIODevice.ReadOnly)
        _sink = QAudioSink(dev, fmt, parent)
        _sink.start(_buf)
        return True
    except Exception:
        return False


def stop_device():
    global _sink, _buf
    try:
        if _sink:
            _sink.stop()
    except Exception:
        pass
    _sink = _buf = None


def beep_wav(seconds=1.2, sr=44100) -> bytes:
    """Short two-tone test sound (attention-tone style, quiet)."""
    n = int(seconds * sr)
    frames = bytearray()
    for i in range(n):
        t = i / sr
        env = min(1.0, t * 40, (seconds - t) * 40)
        v = 0.25 * env * (math.sin(2 * math.pi * 853 * t) + math.sin(2 * math.pi * 960 * t)) / 2
        frames += struct.pack("<h", int(v * 32767))
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return bio.getvalue()
