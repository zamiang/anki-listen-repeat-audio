#!/usr/bin/env python3
"""
Generate Chinese↔English practice audio tracks from Anki cards or text files.

Two modes per entry:
  Recognition: [Chinese] → pause → [English]
  Production:  [English] → pause → [Chinese]

Usage:
  python3 generate-practice-audio.py --source anki --query 'deck:"HSK 1::Claude" tag:batch5'
  python3 generate-practice-audio.py --source file --file docs/chinese-words-phrases-to-add-april-12-2026.txt
  python3 generate-practice-audio.py --source anki --query 'deck:"HSK 1::Claude"' --batch 20

Requires: macOS say + afconvert, ffmpeg (for concatenation).
If using --source anki: Anki running with AnkiConnect (port 8765).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════

ANKI_URL = "http://localhost:8765"

ZH_VOICE = "Meijia (Premium)"  # Taiwan Mandarin
EN_VOICE = "Zoe (Premium)"  # US English

PAUSE_SECONDS = 3          # silence gap for recall
WORKERS = 4

OUTPUT_DIR = "audio-practice"  # relative to working dir

# ══════════════════════════════════════════════════════════════════════
# ANKI CONNECT
# ══════════════════════════════════════════════════════════════════════

def ac(action, **params):
    req = urllib.request.Request(
        ANKI_URL,
        data=json.dumps({"action": action, "version": 6, "params": params}).encode(),
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:
        print(f"ERROR: Cannot reach AnkiConnect at {ANKI_URL}")
        print(f"  Make sure Anki is running with AnkiConnect installed.")
        print(f"  ({e})")
        sys.exit(1)
    if resp.get("error"):
        raise Exception(f"{action}: {resp['error']}")
    return resp["result"]


def fetch_from_anki(query):
    note_ids = ac("findNotes", query=query)
    if not note_ids:
        print(f"No notes found for query: {query}")
        sys.exit(1)
    notes_info = ac("notesInfo", notes=note_ids)
    entries = []
    for note in notes_info:
        fields = note["fields"]
        hanzi = fields.get("Sentence", {}).get("value", "").strip()
        english = fields.get("English", {}).get("value", "").strip()
        pinyin = fields.get("Pinyin", {}).get("value", "").strip()
        if hanzi and english:
            entries.append({
                "hanzi": hanzi,
                "english": english,
                "pinyin": pinyin,
            })
    return entries


# ══════════════════════════════════════════════════════════════════════
# FILE PARSER (same format as import-cards.py)
# ══════════════════════════════════════════════════════════════════════

def parse_file(path):
    with open(path) as f:
        text = f.read()
    entries = []
    blocks = re.split(r"\n(?=\d{4}\n)", text.strip())
    for block in blocks:
        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
        if len(lines) >= 4 and re.match(r"^\d{4}$", lines[0]):
            entries.append({
                "hanzi": lines[3],
                "english": lines[1],
                "pinyin": lines[2],
            })
    return entries


# ══════════════════════════════════════════════════════════════════════
# TTS + AUDIO ASSEMBLY
# ══════════════════════════════════════════════════════════════════════

TTS_SAMPLE_RATE = 22050  # macOS say outputs 22050 Hz mono


def say_to_wav(text, voice, out_path):
    """Generate speech audio using macOS say → WAV (for consistent concat)."""
    aiff = out_path + ".aiff"
    subprocess.run(
        ["say", "-v", voice, "-o", aiff, text],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", aiff, out_path],
        check=True, capture_output=True,
    )
    os.remove(aiff)


def generate_silence(duration_s, out_path):
    """Generate a silent WAV file matching TTS sample rate."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"anullsrc=r={TTS_SAMPLE_RATE}:cl=mono", "-t", str(duration_s),
         out_path],
        check=True, capture_output=True,
    )


def concat_audio(parts, out_path):
    """Concatenate WAV files using ffmpeg concat demuxer, encode to m4a."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p}'\n")
        listfile = f.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", listfile, "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", out_path],
            check=True, capture_output=True,
        )
    finally:
        os.remove(listfile)


def build_track(idx, entry, tmpdir, mode, pause):
    """
    Build a single practice track.
    mode="recognition": Chinese → pause → English
    mode="production":  English → pause → Chinese
    """
    zh_path = os.path.join(tmpdir, f"{idx:04d}_zh.wav")
    en_path = os.path.join(tmpdir, f"{idx:04d}_en.wav")
    silence_path = os.path.join(tmpdir, f"{idx:04d}_silence.wav")

    say_to_wav(entry["hanzi"], ZH_VOICE, zh_path)
    say_to_wav(entry["english"], EN_VOICE, en_path)
    generate_silence(pause, silence_path)

    if mode == "recognition":
        parts = [zh_path, silence_path, en_path]
    else:
        parts = [en_path, silence_path, zh_path]

    return parts, [zh_path, en_path, silence_path]


def build_single_track(args):
    """Worker function: build one entry's WAV parts or assembled m4a.

    Returns (idx, result) where result is either:
      - m4a path (batch=False): fully assembled single track
      - list of WAV paths (batch=True): raw parts for batch assembly
    """
    idx, entry, tmpdir, mode, pause, batch = args
    parts, temps = build_track(idx, entry, tmpdir, mode, pause)
    if batch:
        # Keep WAV parts for batch assembly (caller encodes to m4a)
        return idx, parts
    else:
        out = os.path.join(tmpdir, f"{mode}_{idx:04d}.m4a")
        concat_audio(parts, out)
        for t in temps:
            os.remove(t)
        return idx, out


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Generate Chinese practice audio tracks")
    parser.add_argument("--source", choices=["anki", "file"], required=True,
                        help="Data source: 'anki' (AnkiConnect query) or 'file' (text file)")
    parser.add_argument("--query", help="AnkiConnect search query (required if --source anki)")
    parser.add_argument("--file", help="Path to vocab text file (required if --source file)")
    parser.add_argument("--mode", choices=["recognition", "production", "both"], default="both",
                        help="Which track type(s) to generate (default: both)")
    parser.add_argument("--pause", type=int, default=PAUSE_SECONDS,
                        help=f"Pause duration in seconds (default: {PAUSE_SECONDS})")
    parser.add_argument("--batch", type=int, default=0,
                        help="Batch entries into longer tracks of N items each (0 = individual files)")
    parser.add_argument("--output", default=OUTPUT_DIR,
                        help=f"Output directory (default: {OUTPUT_DIR})")
    args = parser.parse_args()

    pause = args.pause

    # Verify ffmpeg is available
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("ERROR: ffmpeg not found. Install with: brew install ffmpeg")
        sys.exit(1)

    # Load entries
    if args.source == "anki":
        if not args.query:
            print("ERROR: --query required when --source is anki")
            sys.exit(1)
        print(f"Fetching cards from Anki: {args.query}")
        entries = fetch_from_anki(args.query)
    else:
        if not args.file:
            print("ERROR: --file required when --source is file")
            sys.exit(1)
        if not os.path.exists(args.file):
            print(f"ERROR: File not found: {args.file}")
            sys.exit(1)
        entries = parse_file(args.file)

    print(f"Loaded {len(entries)} entries")
    if not entries:
        sys.exit(1)

    modes = ["recognition", "production"] if args.mode == "both" else [args.mode]

    for mode in modes:
        print(f"\n{'='*60}")
        print(f"Generating {mode} tracks...")
        print(f"{'='*60}")

        mode_dir = os.path.join(args.output, mode)
        os.makedirs(mode_dir, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Build tracks in parallel
            t0 = time.time()
            is_batch = args.batch > 0
            tasks = [(i, e, tmpdir, mode, pause, is_batch) for i, e in enumerate(entries)]
            results = {}

            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futures = {ex.submit(build_single_track, t): t[0] for t in tasks}
                done = 0
                for f in as_completed(futures):
                    try:
                        idx, result = f.result()
                        results[idx] = result
                        done += 1
                        if done % 20 == 0 or done == len(entries):
                            print(f"  {done}/{len(entries)} ({time.time()-t0:.0f}s)")
                    except Exception as e:
                        idx = futures[f]
                        print(f"  FAILED entry {idx}: {e}")

            if is_batch:
                # Assemble batches from WAV parts with separator silence
                sep_path = os.path.join(tmpdir, "separator.wav")
                generate_silence(2, sep_path)
                sorted_indices = sorted(results.keys())
                batch_num = 0
                for start in range(0, len(sorted_indices), args.batch):
                    chunk = sorted_indices[start:start + args.batch]
                    batch_num += 1
                    batch_parts = []
                    for j, i in enumerate(chunk):
                        if j > 0:
                            batch_parts.append(sep_path)
                        # results[i] is a list of WAV paths [prompt, silence, answer]
                        batch_parts.extend(results[i])
                    out_path = os.path.join(mode_dir, f"{mode}_batch{batch_num:02d}.m4a")
                    concat_audio(batch_parts, out_path)
                    first_entry = entries[chunk[0]]["hanzi"] if mode == "recognition" else entries[chunk[0]]["english"]
                    print(f"  → {out_path} ({len(chunk)} items, starts with: {first_entry})")
                print(f"  {batch_num} batch files written")
            else:
                # Write individual files (results[i] is an m4a path)
                for idx in sorted(results.keys()):
                    e = entries[idx]
                    # Use hanzi or english as filename depending on mode
                    label = e["hanzi"] if mode == "recognition" else e["english"]
                    # Sanitize filename
                    safe = re.sub(r'[^\w\u4e00-\u9fff\u3400-\u4dbf.-]', '_', label)[:60]
                    out_path = os.path.join(mode_dir, f"{idx+1:03d}_{safe}.m4a")
                    os.rename(results[idx], out_path)
                print(f"  {len(results)} individual files written to {mode_dir}/")

        elapsed = time.time() - t0
        print(f"  Done in {elapsed:.0f}s")

    print(f"\nOutput: {os.path.abspath(args.output)}/")


if __name__ == "__main__":
    main()
