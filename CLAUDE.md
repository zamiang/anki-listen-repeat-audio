# CLAUDE.md

## Project Overview

Python script that generates dual-language listen-and-repeat audio tracks for language learning. Takes vocabulary entries from Anki (via AnkiConnect) or structured text files and produces m4a audio files with prompt → pause → answer structure.

## Repository Structure

```
generate-practice-audio.py   # Main script (stdlib only, no pip deps)
import-cards.py              # Import vocab text files into Anki via AnkiConnect (stdlib only)
revise-cards.py              # Bulk note revision: export→review JSONL→apply (needs opencc)
apply-hanzi-font.py          # Apply LXGW WenKai kaishu font to note type CSS
test_generate.py             # Pytest suite for generate-practice-audio.py
test_import_cards.py         # Pytest suite for import-cards.py pure helpers
test_revise_cards.py         # Pytest suite for revise-cards.py pure helpers
pyproject.toml               # Ruff + pytest config
README.md                    # User-facing docs
CLAUDE.md                    # This file
.github/workflows/ci.yml     # Lint + test on push/PR
```

Generated audio lands in `audio-practice/` by default (configurable via `--output`). The
output directory is gitignored.

## Commands

```bash
# Lint
ruff check generate-practice-audio.py import-cards.py revise-cards.py

# Format check
ruff format --check generate-practice-audio.py

# Format fix
ruff format generate-practice-audio.py

# Run all tests (requires macOS + ffmpeg)
pytest test_generate.py -v

# Run import/revise helper tests (pure Python, no system deps)
pytest test_import_cards.py test_revise_cards.py -v

# Run parser tests only (no system deps)
pytest test_generate.py -v -k "TestParseFile"

# Generate audio from text file
python3 generate-practice-audio.py --source file --file vocab.txt --batch 20

# Generate audio from Anki (requires Anki desktop + AnkiConnect running)
python3 generate-practice-audio.py --source anki --query 'deck:"My Deck"' --batch 20
```

## Architecture

Single-file script, stdlib only (no pip dependencies). Pipeline:

1. **Input** — parse entries from AnkiConnect API or structured text file
2. **TTS** — macOS `say` command generates AIFF, converted to WAV via ffmpeg
3. **Silence** — ffmpeg `anullsrc` generates silence WAVs at matching sample rate (22050 Hz)
4. **Assembly** — ffmpeg concat demuxer joins WAV parts, encodes to AAC m4a with `+faststart`

All intermediate files are WAV to avoid sample rate mismatches during concatenation. Final encode to m4a happens once at the end.

Key constants at the top of the script: `ZH_VOICE`, `EN_VOICE`, `TTS_SAMPLE_RATE`, `PAUSE_SECONDS`, `WORKERS`.

## Content Conventions

- All Chinese content uses **traditional characters, Taiwan standard** (OpenCC `s2twp`
  conventions: 裡 not 裏, 軟體 not 软件). The Anki note type is `ChineseTraditional`.
- Sentence register: natural conversational Taiwan Mandarin at HSK 1-2 level, inspired
  by the Netflix show *Light the Night* (華燈初上). Avoid stiff textbook phrasing.
- TTS voice is Meijia (zh_TW) — traditional is its native script.
- `revise-cards.py` is the only script with a pip dependency (`opencc`, migration
  tooling only); everything else stays stdlib-only.

## Critical Invariant

**TTS_SAMPLE_RATE must match the actual output of the TTS engine.** macOS `say` outputs 22050 Hz. The silence generator uses this same rate. If they differ, ffmpeg concat doubles the duration of the mismatched segments. This was the root cause of a previous timing bug — do not change `TTS_SAMPLE_RATE` without verifying the TTS engine's actual output rate with `ffprobe`.

## Code Style

- Linted and formatted with [ruff](https://docs.astral.sh/ruff/)
- Config in `pyproject.toml`
- Line length: 100
- Target: Python 3.8+

## Audio Output Conventions

- Batch files: `{mode}_batch{NN}.m4a` (20 items per batch by default)
- Individual files: `{NNN}_{label}.m4a`
- 4s recall pause between prompt and answer
- 2s separator silence between batch items
- AAC encoding at 128k with `+faststart` moov atom for streaming playback
