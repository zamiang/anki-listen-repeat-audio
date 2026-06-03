#!/usr/bin/env python3
"""Apply a brush-stroke (kaishu) font to Chinese characters across Anki note types.

Requires Anki running with AnkiConnect (http://localhost:8765).

Idempotent: safe to re-run anytime. It strips any block it previously injected
before re-applying, so running it after an add-on/shared-deck update (which can
overwrite a note type's styling) restores the font in one command:

    python3 apply-hanzi-font.py

The font used is LXGW WenKai (霞鹜文楷), an open-source kaishu font (OFL license).
It is bundled into Anki's media as `_LXGWWenKai-Regular.ttf` so it works on every
device, including phones — the leading underscore stops "Check Media" deleting it.
"""

import json
import re
import sys
import urllib.request

ANKI_URL = "http://localhost:8765"
FONT_FILE = "_LXGWWenKai-Regular.ttf"
FONT_URL = "https://github.com/lxgw/LxgwWenKai/releases/download/v1.522/LXGWWenKai-Regular.ttf"
MARKER = "/* lxgw-wenkai (kaishu) brush font — managed by apply-hanzi-font.py */"
STACK = '"LXGW WenKai", "Kaiti SC", "STKaiti", serif'

# Per note type: the selectors that wrap Chinese characters (NOT pinyin/English).
# To cover a new note type, add its name and its hanzi selector(s) here.
HANZI_SELECTORS = {
    "Basic - New HSK (2025) with sentences - Audio": ".char-card, .char, .char-sim-1, .char-trad-1",
    "Basic - New HSK (2025) with sentences - Meaning": ".char-card, .char, .char-sim-1, .char-trad-1",
    "Basic - New HSK (2025) with sentences - Pinyin": ".char-card, .char, .char-sim-1, .char-trad-1",
    "Basic - New HSK (2025) with sentences - Write": ".char-card, .char, .char-sim-1, .char-trad-1",
    "SpoonFedNote": ".card",
    "ChineseSimplified": ".word, .sentence",
    "HSK Fav": ".hanzi, .hanzi2, .sentenceFront, .sentenceBack",
    "HSK-cef09": ".word, .sentence",
}

FONT_FACE = (
    MARKER + f'\n@font-face {{\n  font-family: "LXGW WenKai";\n  src: url("{FONT_FILE}");\n}}\n'
)

# Matches a previously-injected block: any of our marker comments (the wording
# has varied across versions) through the next "}". Keyed on the stable prefix.
INJECTED_BLOCK = re.compile(r"\n?/\* lxgw-wenkai \(kaishu\) brush font.*?\}\n?", re.DOTALL)


def ac(action, **params):
    req = {"action": action, "version": 6, "params": params}
    try:
        resp = json.load(urllib.request.urlopen(ANKI_URL, data=json.dumps(req).encode()))
    except OSError as e:
        sys.exit(f"ERROR: cannot reach AnkiConnect at {ANKI_URL}. Is Anki running? ({e})")
    if resp.get("error"):
        sys.exit(f"ERROR: AnkiConnect {action}: {resp['error']}")
    return resp["result"]


def ensure_font():
    if ac("getMediaFilesNames", pattern=FONT_FILE):
        print(f"font already in media: {FONT_FILE}")
        return
    print(f"downloading + storing font: {FONT_FILE}")
    ac("storeMediaFile", filename=FONT_FILE, url=FONT_URL)


def apply():
    models = set(ac("modelNames"))
    for model, selectors in HANZI_SELECTORS.items():
        if model not in models:
            print(f"  skip (no such note type): {model}")
            continue
        css = ac("modelStyling", modelName=model)["css"]
        css = INJECTED_BLOCK.sub("", css)  # remove any prior injection
        override = "\n" + MARKER + f"\n{selectors} {{\n  font-family: {STACK} !important;\n}}\n"
        ac("updateModelStyling", model={"name": model, "css": FONT_FACE + css + override})
        print(f"  applied -> {model}  [{selectors}]")


if __name__ == "__main__":
    ensure_font()
    apply()
    print("\nDone. Sync Anki to push the change to your other devices.")
