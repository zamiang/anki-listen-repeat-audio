# generate-practice-audio.py

Generate dual-language audio tracks for active recall practice from Anki cards or text files. Designed for language learning — hear a prompt in one language, pause to recall, then hear the answer.

## How it works

Each vocabulary entry produces two track types:

| Mode | Structure |
|---|---|
| **Recognition** | [Target language] → 4s pause → [English] |
| **Production** | [English] → 4s pause → [Target language] |

Entries can be output as individual files or batched into longer tracks (with 2s silence between items).

## Requirements

- **ffmpeg** — for silence generation, audio concatenation, and AAC encoding
- **edge-tts CLI** — default TTS engine: Azure neural voices (e.g. Taiwan Mandarin
  `zh-TW-HsiaoChenNeural`) via Microsoft Edge's endpoint. Free, no API key, needs
  network.
- **macOS** (optional) — only needed for the offline fallback engine (`--engine say`)
- **Python 3.8+** — stdlib only, no pip dependencies
- **Anki + AnkiConnect** (optional) — only needed if pulling entries from Anki

Install the dependencies:

```bash
brew install ffmpeg
```

```bash
uv tool install edge-tts
```

If using Anki as a source, install the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on (Tools → Add-ons → Get Add-ons → code `2055492159` → restart Anki).

## Quick start

```bash
# From a text file — individual tracks
python3 generate-practice-audio.py --source file --file my-vocab.txt

# From Anki — 20-item batch tracks
python3 generate-practice-audio.py --source anki --query 'deck:"My Deck"' --batch 20

# Production mode only, 5s recall pause
python3 generate-practice-audio.py --source file --file my-vocab.txt --mode production --pause 5
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--source` | *(required)* | `anki` or `file` |
| `--query` | — | AnkiConnect search query (required for `--source anki`) |
| `--file` | — | Path to text file (required for `--source file`) |
| `--mode` | `both` | `recognition`, `production`, or `both` |
| `--engine` | `edge` | TTS engine: `edge` (Azure neural voices, needs network) or `say` (macOS, offline) |
| `--pause` | `4` | Seconds of silence for recall |
| `--batch` | `0` | Items per batch track (0 = one file per entry) |
| `--output` | `audio-practice` | Output directory |
| `--album` | `Chinese Practice` | Album name in m4a metadata (each mode becomes its own album — see below) |
| `--group` | off | (Anki only) Cluster example sentences under their word anchors. Off by default so audio follows Anki note order — see below. |

## Input formats

### Anki (via AnkiConnect)

The script queries AnkiConnect for notes matching your search. It expects the
**`ChineseTraditional`** note type, with these fields:

| Field | Purpose |
|---|---|
| `Sentence` | Target language text (used as prompt/answer) |
| `English` | English translation |
| `Pinyin` | Romanization (not used in audio, but read from notes) |
| `Word` | Vocabulary word (only used by `--group`) |

Notes lacking `Sentence`/`English` (i.e. a different note type) are skipped with a
warning that lists the fields actually found; if a query matches **no** usable notes
the script exits with an error rather than producing silent/empty output. Scope your
query to the right note type, e.g. `--query 'note:ChineseTraditional deck:"My Deck"'`.

By default, audio follows the note order Anki returns (sorted by note id), so tracks
line up with study order. Pass `--group` to instead cluster each example sentence
after the word anchor it contains — note that this matches by substring, so common
characters (了, 吗, …) can pull in unrelated sentences.

Anki must be running with AnkiConnect listening on `http://localhost:8765`.

### Text file

Same format as the companion `import-cards.py` script:

```
0001
Hello
nǐ hǎo
你好

0002
Thank you
xiè xiè
謝謝
```

Pattern per entry: 4-digit ID, English, romanization, target language text. Separated by blank lines.

## Output

```
audio-practice/
  recognition/                     # [Target] → pause → [English]
    recognition_batch01.m4a        #   batch mode: N items per file
    recognition_batch02.m4a
    ...
  production/                      # [English] → pause → [Target]
    001_你好.m4a                    #   individual mode: one file per entry
    002_謝謝.m4a
    ...
```

### Metadata for music apps

Every m4a is tagged with iTunes-style metadata so it sorts and groups cleanly in
Apple Music and other players:

- **Album** — `{--album} — Recognition` / `{--album} — Production`, so the two
  directions group as separate albums (default base: `Chinese Practice`).
- **Track number** — sequential within each album (batch number in batch mode,
  entry number in individual mode), so tracks play in study order.
- **Title** — the prompt text (or `Batch NN — <first item>` for batches).
- **Artist / Album artist** — `Anki Listen & Repeat`; **Genre** — `Language Learning`.

## Companion scripts

- **`import-cards.py`** — imports vocabulary from text files into Anki (TTS audio,
  content-hash media names, theme tagging). Stdlib only.
- **`revise-cards.py`** — bulk revision tool: exports all claude-tagged notes to a
  reviewable JSONL (with an OpenCC simplified→traditional Taiwan-standard baseline),
  then applies the edited file back to Anki with regenerated audio. Requires
  `pip3 install opencc` (the only script with a pip dependency).
- **`apply-hanzi-font.py`** — applies the LXGW WenKai kaishu font to Chinese text
  across note types.

## Adapting for other languages

To change languages, edit the voice constants at the top of the script:

```python
# edge engine (default) — Azure neural voices
EDGE_ZH_VOICE = "zh-TW-HsiaoChenNeural"  # ← your target language voice
EDGE_EN_VOICE = "en-US-AvaNeural"  # ← your native language voice

# say engine (offline fallback) — macOS voices
ZH_VOICE = "Meijia (Premium)"
EN_VOICE = "Zoe (Premium)"
```

List available voices for each engine:

```bash
edge-tts --list-voices
```

```bash
say -v '?'
```

Some useful macOS voices:

| Language | Voice |
|---|---|
| Japanese | Kyoko, O-Ren |
| Korean | Yuna |
| French | Thomas, Amelie |
| Spanish | Paulina (MX), Monica (ES) |
| German | Anna |
| Mandarin (Taiwan) | Meijia |
| Mandarin (Mainland) | Tingting |
| Cantonese | Sinji |

Premium voices (e.g. `"Meijia (Premium)"`) sound significantly better but must be downloaded first in System Settings → Accessibility → Spoken Content → System Voice → Manage Voices.

### Anki field mapping

If your Anki note type uses different field names, update `fetch_from_anki()`:

```python
hanzi = fields.get("Sentence", {}).get("value", "")  # ← your "front" field
english = fields.get("English", {}).get("value", "")  # ← your "back" field
```

### TTS sample rate

Every TTS clip is resampled to `TTS_SAMPLE_RATE` (22050 Hz) during WAV conversion, and silence files are generated at the same rate — mismatched rates distort durations during concatenation. If you add a new TTS engine, route its output through the same `-ar`/`-ac` ffmpeg conversion.

## Performance

~1 second per entry (TTS generation is the bottleneck). 4 parallel workers by default. A 200-entry deck takes a few minutes to generate both recognition and production tracks.
