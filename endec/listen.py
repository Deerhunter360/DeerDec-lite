"""Plays one monitored source out loud. Decoding always runs; playback is optional
(manager.listen_name = None keeps everything silent)."""
import queue
import time

from PySide6.QtCore import QObject, QTimer


from . import audio_out


class AudioListener(QObject):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.m, self.volume = manager, 0.8
        self.sink = self.dev = self.sr = None
        self.pending = bytearray()
        self._gen, self._idle = 0, None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain)
        self.timer.start(30)

    def set_volume(self, v):
        self.volume = max(0.0, min(1.0, v))
        if self.sink:
            self.sink.setVolume(self.volume)

    def _open(self, sr):
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
        self._close()
        fmt = QAudioFormat()
        fmt.setSampleRate(int(sr))
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        from . import audio_out
        out = audio_out.pick_output(self.m.cfg.audio_output)
        if out.isNull():
            raise RuntimeError("no audio output device")
        self.m.engine.log(f"Listen: opening {out.description()} at {int(sr)} Hz "
                          f"(format supported: {out.isFormatSupported(fmt)})")
        self.sink = QAudioSink(out, fmt, self)
        self.sink.setBufferSize(int(sr) * 2 * 4)      # ~4 s
        self.sink.setVolume(self.volume)
        self.dev, self.sr = self.sink.start(), int(sr)

    def _close(self):
        if self.sink:
            self.sink.stop()
        self.sink = self.dev = self.sr = None
        self.pending = bytearray()

    def _drain(self):
        if self._gen != self.m.flush_gen:             # user changed/muted the source: drop what is buffered
            self._gen = self.m.flush_gen
            self._close()
        if self.m.listen_name is None:
            if not self.sink:
                return
            # relay just ended: let queued + buffered audio finish instead of cutting it off
            self._idle = self._idle or time.time()
            busy = self.pending or not self.m.listen_q.empty() or self.sink.bytesFree() < self.sink.bufferSize()
            if not busy or time.time() - self._idle > 60:
                self._idle = None
                self._close()
                return
        else:
            self._idle = None
        try:
            while True:
                sr, data = self.m.listen_q.get_nowait()
                audio_out.tap_pcm(data, sr)                    # live relay audio also goes to the stream
                if self.sr != sr or not self.sink:
                    self._open(sr)
                self.pending += data
        except queue.Empty:
            pass
        except Exception as e:
            self.m.engine.log(f"Listen error: {e}")
            self.m.set_listen(None)
            self._close()
            return
        try:
            if self.sink and self.pending:                # write what fits, keep the rest for next tick
                n = min(len(self.pending), self.sink.bytesFree()) // 2 * 2
                if n:
                    self.dev.write(bytes(self.pending[:n]))
                    del self.pending[:n]
        except Exception as e:
            self.m.engine.log(f"Listen error: {e}")
            self.m.set_listen(None)
            self._close()
