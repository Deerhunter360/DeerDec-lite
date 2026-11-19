# DeerDec Lite

A small **EAS alert viewer / relay** for Windows (Python + PySide6). It watches CAP alert feeds such as FEMA
IPAWS-OPEN, plays each alert with an attention tone and voice (or the audio the alert comes with), and shows it
on a full-screen CGEN. There is also a live alert map. Hobby / online use only.

> **Not a certified EAS device.** Do not use it for public alerting and **never feed its audio into any broadcast,
> cable or over-the-air path** (in the US: 47 CFR Part 11 and the FCC rules on false alerts and EAS tones).
> It can miss or mis-handle alerts; do not rely on it for your safety.

## What it does
- **Monitor** - feed status, log, alert queue, ABORT, screen style choice.
- **Sources** - the FEMA IPAWS feed plus any IPAWS-style CAP feed you add (**Add CAP feed...**); switch feeds on or off.
- **CGEN** - EASyPLUS, DASDEC-II, EASyCAP, VDS, CEMS-1000, Xfinity, PSC-902, Atari 800XL, Crawl. Keys 1-9 / C / D / W
  change style on the alert screen; Esc or a click closes it. ABORT is Ctrl+Shift+A.
- **Map** - active alerts as NWS polygons or shaded counties; expired alerts leave on their own.
- Level correction and a choice of speaker. Config has the rest.

## Setup (Windows)
    setup.bat                      # or: pip install -r requirements.txt
    python app.py                  # or double-click DeerDec.bat

Python 3.10+. The map needs internet for tiles and (once) county outlines. Fonts go in `fonts/` (optional):
`eas.ttf` (EASyPLUS), `luximr/luximb/luximri/luximbi.ttf` (DASDEC-II), `atari.ttf` (Atari 800XL).
SAME headers contain no year, so the DASDEC screen prints the current year.

## AI disclosure
DeerDec was written with heavy help from AI (Anthropic's Claude) and directed by its author. Expect bugs, read the
code before trusting it, and check anything that matters against official sources.

## Credits
`endec/defs.py` is from dsame by Joseph W. Metcalf (ISC license, see the file header). Screen designs imitate hardware
endecs for hobby display only; DeerDec is not affiliated with their makers. Alert data: FEMA IPAWS-OPEN, the NWS API,
US Census county outlines. Map tiles: Esri. No licence file is included.
