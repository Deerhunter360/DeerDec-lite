"""Runs in its own process (avoids Windows COM/thread problems):
   python tts_worker.py <text_file> <out.wav> <rate> [voice_hint]"""
import sys

import pyttsx3

text = open(sys.argv[1], encoding="utf-8").read()
out, rate = sys.argv[2], int(sys.argv[3])
hint = sys.argv[4].lower() if len(sys.argv) > 4 else ""
eng = pyttsx3.init()
eng.setProperty("rate", rate)
if hint:
    for v in eng.getProperty("voices"):
        if hint in v.name.lower():
            eng.setProperty("voice", v.id)
            break
eng.save_to_file(text, out)
eng.runAndWait()
