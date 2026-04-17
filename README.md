# generate-practice-audio.py

Generate dual-language audio tracks for active recall practice from Anki cards or text files. Designed for language learning — hear a prompt in one language, pause to recall, then hear the answer.

## How it works

Each vocabulary entry produces two track types:

| Mode | Structure |
|---|---|
| **Recognition** | [Target language] → 3s pause → [English] |
| **Production** | [English] → 3s pause → [Target language] |

Entries can be output as individual files or batched into longer tracks (with 2s silence between items).

## Requirements

- **macOS** — uses the built-in `say` command for TTS
- **ffmpeg** — for silence generation, audio concatenation, and AAC encoding
- **Python 3.6+** — stdlib only, no pip dependencies
- **Anki + AnkiConnect** (optional) — only needed if pulling entries from Anki

Install ffmpeg:

```bash
brew install ffmpeg
```

If using Anki as a source, install the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on (Tools → Add-ons → Get Add-ons → code `2055492159` → restart Anki).

## Quick start

```bash
# From a text file — individual tracks
python3 scripts/generate-practice-audio.py --source file --file my-vocab.txt

# From Anki — 20-item batch tracks
python3 scripts/generate-practice-audio.py --source anki --query 'deck:"My Deck"' --batch 20

# Production mode only, 5s recall pause
python3 scripts/generate-practice-audio.py --source file --file my-vocab.txt --mode production --pause 5
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--source` | *(required)* | `anki` or `file` |
| `--query` | — | AnkiConnect search query (required for `--source anki`) |
| `--file` | — | Path to text file (required for `--source file`) |
| `--mode` | `both` | `recognition`, `production`, or `both` |
| `--pause` | `3` | Seconds of silence for recall |
| `--batch` | `0` | Items per batch track (0 = one file per entry) |
| `--output` | `audio-practice` | Output directory |

## Input formats

### Anki (via AnkiConnect)

The script queries AnkiConnect for notes matching your search. It expects notes with these fields:

| Field | Purpose |
|---|---|
| `Sentence` | Target language text (used as prompt/answer) |
| `English` | English translation |
| `Pinyin` | Romanization (not used in audio, but read from notes) |

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
谢谢
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
    002_谢谢.m4a
    ...
```

## Adapting for other languages

The script uses macOS TTS voices. To change languages, edit the constants at the top of the script:

```python
ZH_VOICE = "Meijia (Premium)"  # ← change to your target language voice
EN_VOICE = "Zoe (Premium)"     # ← change to your native language voice
```

List available voices:

```bash
say -v '?'
```

Some useful voices:

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
hanzi = fields.get("Sentence", {}).get("value", "")    # ← your "front" field
english = fields.get("English", {}).get("value", "")    # ← your "back" field
```

### TTS sample rate

macOS `say` outputs at 22050 Hz. The script generates silence files at this same rate to avoid duration distortion during concatenation. If you replace the TTS engine, update `TTS_SAMPLE_RATE` to match your engine's output rate.

## Performance

~1 second per entry (TTS generation is the bottleneck). 4 parallel workers by default. A 200-entry deck takes ~4 minutes to generate both recognition and production tracks.
