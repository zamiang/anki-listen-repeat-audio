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
# Lint (discovers all Python files via pyproject.toml, matching CI)
ruff check

# Format check
ruff format --check

# Format fix
ruff format

# Run all tests (requires macOS + ffmpeg)
pytest test_generate.py -v

# Run import/revise helper tests (pure Python, no system deps)
pytest test_import_cards.py test_revise_cards.py -v

# Run parser tests only (no system deps)
pytest test_generate.py -v -k "TestParseFile"

# Generate audio from text file (edge engine by default; needs edge-tts CLI + network)
python3 generate-practice-audio.py --source file --file vocab.txt --batch 20

# Generate audio from Anki (requires Anki desktop + AnkiConnect running)
python3 generate-practice-audio.py --source anki --query 'deck:"My Deck"' --batch 20

# Offline fallback engine (macOS say voices)
python3 generate-practice-audio.py --source file --file vocab.txt --engine say
```

## Architecture

Single-file script, stdlib only (no pip dependencies). Pipeline:

1. **Input** — parse entries from AnkiConnect API or structured text file
2. **TTS** — two engines behind `--engine` (dispatched by `tts_to_wav`):
   - `edge` (default) — `edge-tts` CLI (Azure neural voices, network required,
     install via `uv tool install edge-tts`) generates 24 kHz MP3
   - `say` (offline fallback) — macOS `say` generates AIFF
   Either output is converted to WAV via ffmpeg with explicit `-ar TTS_SAMPLE_RATE -ac 1`
3. **Silence** — ffmpeg `anullsrc` generates silence WAVs at matching sample rate (22050 Hz)
4. **Assembly** — ffmpeg concat demuxer joins WAV parts, encodes to AAC m4a with `+faststart`

All intermediate files are WAV to avoid sample rate mismatches during concatenation. Final encode to m4a happens once at the end. The script stays stdlib-only by shelling out to the `edge-tts` CLI (like `say`/`ffmpeg`) rather than importing the package.

Key constants at the top of the script: `EDGE_ZH_VOICE`, `EDGE_EN_VOICE`, `ZH_VOICE`, `EN_VOICE`, `TTS_SAMPLE_RATE`, `PAUSE_SECONDS`, `WORKERS`.

## Content Conventions

- All Chinese content uses **traditional characters, Taiwan standard** (OpenCC `s2twp`
  conventions: 裡 not 裏, 軟體 not 软件). The Anki note type is `ChineseTraditional`.
- Sentence register: natural conversational Taiwan Mandarin at HSK 1-2 level, inspired
  by the Netflix show *Light the Night* (華燈初上). Avoid stiff textbook phrasing.
- TTS voices are `zh-TW-HsiaoChenNeural` (edge engine, default) and Meijia (say
  fallback) — both Taiwan Mandarin, traditional is their native script.
- `revise-cards.py` is the only script with a pip dependency (`opencc`, migration
  tooling only); everything else stays stdlib-only.

## Critical Invariant

**Every WAV part must be at TTS_SAMPLE_RATE before concat.** Both engines' TTS→WAV conversions force `-ar TTS_SAMPLE_RATE -ac 1` (edge-tts natively outputs 24 kHz, `say` 22050 Hz), and the silence generator uses the same rate. If any part's rate differs, ffmpeg concat distorts the duration of the mismatched segments — this was the root cause of a previous timing bug. When adding a TTS engine, route its output through the same forced-resample conversion; do not change `TTS_SAMPLE_RATE` without verifying every part still matches via `ffprobe`.

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
