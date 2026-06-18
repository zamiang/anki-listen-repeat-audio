#!/usr/bin/env python3
"""
Generate Chinese↔English practice audio tracks from Anki cards or text files.

Two modes per entry:
  Recognition: [Chinese] → pause → [English]
  Production:  [English] → pause → [Chinese]

Usage:
  python3 generate-practice-audio.py --source file --file vocab.txt
  python3 generate-practice-audio.py --source anki --query 'deck:"HSK 1::Claude"' --batch 20

Requires: macOS say, ffmpeg (for silence generation, concatenation, and AAC encoding).
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

PAUSE_SECONDS = 4  # silence gap for recall
WORKERS = 4

OUTPUT_DIR = "audio-practice"  # relative to working dir

# iTunes/Music metadata written into every generated m4a (see build_metadata).
ALBUM_ARTIST = "Anki Listen & Repeat"
GENRE = "Language Learning"
DEFAULT_ALBUM = "Chinese Practice"

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
        print("  Make sure Anki is running with AnkiConnect installed.")
        print(f"  ({e})")
        sys.exit(1)
    if resp.get("error"):
        raise Exception(f"{action}: {resp['error']}")
    return resp["result"]


def notes_to_entries(notes_info):
    """Map AnkiConnect notesInfo results to entries (ChineseTraditional note type).

    Returns (entries, skipped). A note is skipped when it lacks the required
    'Sentence' and 'English' fields — e.g. a query that matched a different note
    type. The caller is responsible for warning the user about skips.
    """
    entries = []
    skipped = 0
    for note in notes_info:
        fields = note["fields"]
        hanzi = fields.get("Sentence", {}).get("value", "").strip()
        english = fields.get("English", {}).get("value", "").strip()
        pinyin = fields.get("Pinyin", {}).get("value", "").strip()
        word = fields.get("Word", {}).get("value", "").strip()
        if hanzi and english:
            entries.append(
                {
                    "hanzi": hanzi,
                    "english": english,
                    "pinyin": pinyin,
                    "word": word,
                }
            )
        else:
            skipped += 1
    return entries, skipped


def fetch_from_anki(query, group=False):
    note_ids = ac("findNotes", query=query)
    if not note_ids:
        print(f"No notes found for query: {query}")
        sys.exit(1)
    # findNotes returns an unordered set; sort by note id (creation order) so a
    # given query always produces the same track sequence run-to-run.
    notes_info = ac("notesInfo", notes=sorted(note_ids))
    entries, skipped = notes_to_entries(notes_info)

    if skipped:
        found_fields = sorted(notes_info[0]["fields"].keys()) if notes_info else []
        print(
            f"WARNING: skipped {skipped}/{len(notes_info)} notes lacking 'Sentence'/'English' "
            "fields (wrong note type?)."
        )
        print(f"  Fields on first matched note: {found_fields}")
        print(
            "  This script expects the 'ChineseTraditional' note type "
            "(fields: Sentence, Word, Pinyin, English)."
        )
    if not entries:
        print(
            "ERROR: no matched notes had usable Sentence + English fields. "
            "Narrow your --query to ChineseTraditional notes (e.g. add 'note:ChineseTraditional')."
        )
        sys.exit(1)

    # Grouping reorders sentences under word anchors via substring matching, which
    # scrambles study order and mis-clusters common characters — off by default.
    return group_by_anchor(entries) if group else entries


def group_by_anchor(entries):
    """Reorder for word+examples study flow.

    Anchors (entries with a non-empty 'word') keep their input order. Each
    non-anchor whose 'hanzi' contains some anchor's 'word' is placed right after
    that anchor; longer anchor words win ambiguous matches (so 大学生 beats 学生).
    Non-anchors with no matching word appear at the end in input order.
    """
    anchors = [e for e in entries if e.get("word")]
    non_anchors = [e for e in entries if not e.get("word")]
    if not anchors:
        return list(entries)

    # Longest word first so 大学生 wins over 学生 for ambiguous orphans.
    words_by_length = sorted({a["word"] for a in anchors}, key=len, reverse=True)

    attached = {a["word"]: [] for a in anchors}
    unattached = []
    for na in non_anchors:
        match = next((w for w in words_by_length if w in na["hanzi"]), None)
        if match:
            attached[match].append(na)
        else:
            unattached.append(na)

    result = []
    seen = set()
    for a in anchors:
        result.append(a)
        w = a["word"]
        if w not in seen:
            result.extend(attached[w])
            seen.add(w)
    result.extend(unattached)
    return result


# ══════════════════════════════════════════════════════════════════════
# FILE PARSER (same format as import-cards.py)
# ══════════════════════════════════════════════════════════════════════


def parse_file(path):
    with open(path) as f:
        text = f.read()
    entries = []
    blocks = re.split(r"\n(?=\d{4}\n)", text.strip())
    for block in blocks:
        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        if len(lines) >= 4 and re.match(r"^\d{4}$", lines[0]):
            entries.append(
                {
                    "hanzi": lines[3],
                    "english": lines[1],
                    "pinyin": lines[2],
                }
            )
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
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", aiff, out_path],
        check=True,
        capture_output=True,
    )
    os.remove(aiff)


def generate_silence(duration_s, out_path):
    """Generate a silent WAV file matching TTS sample rate."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={TTS_SAMPLE_RATE}:cl=mono",
            "-t",
            str(duration_s),
            out_path,
        ],
        check=True,
        capture_output=True,
    )


def concat_audio(parts, out_path, metadata=None):
    """Concatenate WAV files using ffmpeg concat demuxer, encode to m4a.

    Output is resampled to 44.1 kHz — iTunes/Music can mishandle non-standard
    AAC sample rates (the WAV inputs are 22050 Hz to match macOS `say`).

    metadata: optional dict of iTunes-style tags (title, album, artist, track,
    genre, ...) written into the m4a so music apps can sort/group the tracks.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p}'\n")
        listfile = f.name
    meta_args = []
    for key, value in (metadata or {}).items():
        if value:
            meta_args += ["-metadata", f"{key}={value}"]
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                listfile,
                "-ar",
                "44100",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                *meta_args,
                "-movflags",
                "+faststart",
                out_path,
            ],
            check=True,
            capture_output=True,
        )
    finally:
        os.remove(listfile)


def build_metadata(album, mode, title, track, total):
    """iTunes-style tags so music apps can sort/group the practice tracks.

    Each mode gets its own album (e.g. "Chinese Practice — Recognition") so
    recognition and production tracks group separately, ordered by track number.
    """
    return {
        "album": f"{album} — {mode.title()}",
        "album_artist": ALBUM_ARTIST,
        "artist": ALBUM_ARTIST,
        "genre": GENRE,
        "title": title,
        "track": f"{track}/{total}",
        "comment": f"{mode} practice track",
    }


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
    idx, entry, tmpdir, mode, pause, batch, album, total = args
    parts, temps = build_track(idx, entry, tmpdir, mode, pause)
    if batch:
        # Keep WAV parts for batch assembly (caller encodes to m4a)
        return idx, parts
    else:
        out = os.path.join(tmpdir, f"{mode}_{idx:04d}.m4a")
        title = entry["hanzi"] if mode == "recognition" else entry["english"]
        meta = build_metadata(album, mode, title, idx + 1, total)
        concat_audio(parts, out, metadata=meta)
        for t in temps:
            os.remove(t)
        return idx, out


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Generate Chinese practice audio tracks")
    parser.add_argument(
        "--source",
        choices=["anki", "file"],
        required=True,
        help="Data source: 'anki' (AnkiConnect query) or 'file' (text file)",
    )
    parser.add_argument("--query", help="AnkiConnect search query (required if --source anki)")
    parser.add_argument("--file", help="Path to vocab text file (required if --source file)")
    parser.add_argument(
        "--mode",
        choices=["recognition", "production", "both"],
        default="both",
        help="Which track type(s) to generate (default: both)",
    )
    parser.add_argument(
        "--pause",
        type=int,
        default=PAUSE_SECONDS,
        help=f"Pause duration in seconds (default: {PAUSE_SECONDS})",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=0,
        help="Batch entries into longer tracks of N items each (0 = individual files)",
    )
    parser.add_argument(
        "--output", default=OUTPUT_DIR, help=f"Output directory (default: {OUTPUT_DIR})"
    )
    parser.add_argument(
        "--album",
        default=DEFAULT_ALBUM,
        help="Album name written to m4a metadata; each mode becomes "
        f'"{{album}} — Recognition/Production" (default: "{DEFAULT_ALBUM}")',
    )
    parser.add_argument(
        "--group",
        action="store_true",
        help="(Anki only) Cluster example sentences under their word anchors. "
        "Default: preserve Anki note order so audio lines up with study order.",
    )
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
        entries = fetch_from_anki(args.query, group=args.group)
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
        print(f"\n{'=' * 60}")
        print(f"Generating {mode} tracks...")
        print(f"{'=' * 60}")

        mode_dir = os.path.join(args.output, mode)
        os.makedirs(mode_dir, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Build tracks in parallel
            t0 = time.time()
            is_batch = args.batch > 0
            tasks = [
                (i, e, tmpdir, mode, pause, is_batch, args.album, len(entries))
                for i, e in enumerate(entries)
            ]
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
                            print(f"  {done}/{len(entries)} ({time.time() - t0:.0f}s)")
                    except Exception as e:
                        idx = futures[f]
                        print(f"  FAILED entry {idx}: {e}")

            if is_batch:
                # Assemble batches from WAV parts with separator silence
                sep_path = os.path.join(tmpdir, "separator.wav")
                generate_silence(2, sep_path)
                sorted_indices = sorted(results.keys())
                total_batches = (len(sorted_indices) + args.batch - 1) // args.batch
                batch_num = 0
                for start in range(0, len(sorted_indices), args.batch):
                    chunk = sorted_indices[start : start + args.batch]
                    batch_num += 1
                    batch_parts = []
                    for j, i in enumerate(chunk):
                        if j > 0:
                            batch_parts.append(sep_path)
                        # results[i] is a list of WAV paths [prompt, silence, answer]
                        batch_parts.extend(results[i])
                    out_path = os.path.join(mode_dir, f"{mode}_batch{batch_num:02d}.m4a")
                    first_entry = (
                        entries[chunk[0]]["hanzi"]
                        if mode == "recognition"
                        else entries[chunk[0]]["english"]
                    )
                    meta = build_metadata(
                        args.album,
                        mode,
                        f"Batch {batch_num:02d} — {first_entry}",
                        batch_num,
                        total_batches,
                    )
                    concat_audio(batch_parts, out_path, metadata=meta)
                    print(f"  → {out_path} ({len(chunk)} items, starts with: {first_entry})")
                print(f"  {batch_num} batch files written")
            else:
                # Write individual files (results[i] is an m4a path)
                for idx in sorted(results.keys()):
                    e = entries[idx]
                    # Use hanzi or english as filename depending on mode
                    label = e["hanzi"] if mode == "recognition" else e["english"]
                    # Sanitize filename
                    safe = re.sub(r"[^\w\u4e00-\u9fff\u3400-\u4dbf.-]", "_", label)[:60]
                    out_path = os.path.join(mode_dir, f"{idx + 1:03d}_{safe}.m4a")
                    os.rename(results[idx], out_path)
                print(f"  {len(results)} individual files written to {mode_dir}/")

        elapsed = time.time() - t0
        print(f"  Done in {elapsed:.0f}s")

    print(f"\nOutput: {os.path.abspath(args.output)}/")


if __name__ == "__main__":
    main()
