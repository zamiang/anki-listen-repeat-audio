#!/usr/bin/env python3
"""
Import Chinese flashcards (traditional characters, Taiwan standard) from a
structured text file into Anki via AnkiConnect. Notes use the ChineseTraditional
note type.

Usage:
    python3 import-cards.py <file.txt>                    # import a batch
    python3 import-cards.py --export-known-words [out]    # dump hanzi for Migaku
    python3 import-cards.py --regenerate-audio            # re-TTS all claude-tagged notes
    python3 import-cards.py --audit                       # report audio↔text mismatches
    python3 import-cards.py --repair                      # fix mismatches: migrate to content-hash audio

Requires: Anki running with AnkiConnect (port 8765), macOS say + afconvert.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit these as needed
# ══════════════════════════════════════════════════════════════════════

ANKI_URL = "http://localhost:8765"
DECK = "HSK 1::Claude"
MODEL = "ChineseTraditional"

VOICE = "Meijia"  # Taiwan Mandarin (zh_TW)
WORKERS = 4  # parallel TTS threads
FILENAME_PREFIX = "claude"  # media: claude_<sha1(sentence)[:10]>.m4a

HANZI_CONVERSIONS = {
    "哪兒": "哪裡",
    "這兒": "這裡",
    "那兒": "那裡",
}

PINYIN_CONVERSIONS = {
    "nǎr": "nǎlǐ",
    "zhèr": "zhèlǐ",
    "nàr": "nàlǐ",
    "Nǎr": "Nǎlǐ",
    "Zhèr": "Zhèlǐ",
    "Nàr": "Nàlǐ",
}

GLOBAL_TAGS = ["claude"]

# Decks to scan for the Migaku known-words export.
# Spoonfed is intentionally omitted: its Word field is empty (sentence-only notes),
# and Migaku's importer expects one word per line, not phrases.
# xiehanzi: only the ::Meaning subdeck — Write/Audio/Pinyin are duplicate notes of the same chars.
KNOWN_WORDS_DECKS = [
    "HSK 1::Claude",
    "Anki-xiehanzi - New HSK (2025) with sentences::HSK 1::Meaning",
]
# Field names to try (in order) for the vocab/word value on each note.
KNOWN_WORDS_FIELDS = ["Word", "Traditional", "Simplified", "Hanzi", "Character", "Front"]
KNOWN_WORDS_DEFAULT_OUTPUT = "migaku_known_words.txt"

# Theme detection: (keywords_in_english, tag_name)
# First match wins. Checked against lowercased English field.
THEME_RULES = [
    (
        [
            "month",
            "week",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
            "january",
            "february",
            "march",
        ],
        "calendar",
    ),
    (["hour", "minute", "second", "year", "day"], "time"),
    (
        [
            "big",
            "small",
            "hot",
            "cold",
            "young",
            "old",
            "tall",
            "short",
            "heavy",
            "light",
            "fast",
            "slow",
            "however",
            "but",
        ],
        "adjectives",
    ),
    (["like", "enjoy", "love", "prefer"], "preferences"),
    (["right", "correct"], "confirmation"),
    (["child", "children", "kid"], "family"),
    (["understand", "know"], "comprehension"),
]

# ══════════════════════════════════════════════════════════════════════
# ANKI CONNECT HELPER
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


def media_filename(sentence):
    """Content-addressed media name: identical text → identical file, never collides.

    Replaces the old positional claude_batch{N}_{idx}.m4a scheme, whose
    uniqueness depended on hand-bumping a global counter.
    """
    digest = hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:10]
    return f"{FILENAME_PREFIX}_{digest}.m4a"


def source_tag(path):
    """Derive a 'src:<stem>' Anki tag from the input filename.

    Replaces the old batchN tag (also keyed off a hand-edited global counter).
    Whitespace is collapsed to hyphens because Anki tags cannot contain spaces.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    safe = re.sub(r"\s+", "-", stem.strip())
    return f"src:{safe}"


def find_collisions(notes):
    """Return {filename: sorted distinct sentences} for any media file referenced
    by more than one distinct sentence.

    Input is AnkiConnect notesInfo results. An empty dict means the collection is
    clean. Notes with no [sound:...] reference are ignored.
    """
    from collections import defaultdict

    fn_to_sentences = defaultdict(set)
    for note in notes:
        fields = note.get("fields", {})
        sentence = fields.get("Sentence", {}).get("value", "").strip()
        m = re.search(r"\[sound:([^\]]+)\]", fields.get("Audio", {}).get("value", ""))
        if not m:
            continue
        fn_to_sentences[m.group(1)].add(sentence)
    return {fn: sorted(s) for fn, s in fn_to_sentences.items() if len(s) > 1}


def repair_targets(notes):
    """Notes whose Audio filename differs from the content-hash name for their Sentence.

    Input is AnkiConnect notesInfo results. Returns a list of
    {"id", "sentence", "filename"} dicts identifying notes that need their audio
    regenerated and re-pointed at a content-hash filename. Notes without a Sentence
    are skipped. A note already on the correct content-hash name is not a target,
    so repair is idempotent (a second run finds nothing to do).
    """
    targets = []
    for note in notes:
        fields = note.get("fields", {})
        sentence = fields.get("Sentence", {}).get("value", "").strip()
        if not sentence:
            continue
        new_fn = media_filename(sentence)
        m = re.search(r"\[sound:([^\]]+)\]", fields.get("Audio", {}).get("value", ""))
        current_fn = m.group(1) if m else None
        if current_fn != new_fn:
            targets.append({"id": note.get("noteId"), "sentence": sentence, "filename": new_fn})
    return targets


# ══════════════════════════════════════════════════════════════════════
# STEP 1: PARSE
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
                    "id": lines[0],
                    "english": lines[1],
                    "pinyin": lines[2],
                    "hanzi": lines[3],
                }
            )
    return entries


# ══════════════════════════════════════════════════════════════════════
# STEP 2: CONVERT 兒→裡
# ══════════════════════════════════════════════════════════════════════


def convert_er(entries):
    count = 0
    for e in entries:
        orig = e["hanzi"]
        for old, new in HANZI_CONVERSIONS.items():
            e["hanzi"] = e["hanzi"].replace(old, new)
        for old, new in PINYIN_CONVERSIONS.items():
            e["pinyin"] = e["pinyin"].replace(old, new)
        if e["hanzi"] != orig:
            count += 1
    return count


# ══════════════════════════════════════════════════════════════════════
# STEP 3: CLASSIFY (word vs sentence, theme tags)
# ══════════════════════════════════════════════════════════════════════


def is_cjk(c):
    cp = ord(c)
    return 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF


def classify(entry):
    h = entry["hanzi"]
    eng = entry["english"].lower()
    cjk = [c for c in h if is_cjk(c)]
    punctuation = "。？！，"

    # Word field: bare vocab (≤2 CJK, no punctuation)
    is_bare = len(cjk) <= 2 and not any(p in h for p in punctuation)
    word = h if is_bare else ""

    # Theme tag: first matching rule wins
    tag = "sentence"  # default
    for keywords, theme in THEME_RULES:
        if any(kw in eng for kw in keywords):
            tag = theme
            break
    # Fallback: short entries without a theme match → "numbers" if all digits/CJK
    if tag == "sentence" and is_bare and len(cjk) <= 3:
        tag = "numbers"

    return word, tag


# ══════════════════════════════════════════════════════════════════════
# STEP 4: GENERATE AUDIO (parallel)
# ══════════════════════════════════════════════════════════════════════


def gen_audio(idx_sentence):
    idx, sentence = idx_sentence
    aiff = f"/tmp/import_{idx:04d}.aiff"
    m4a = f"/tmp/import_{idx:04d}.m4a"
    subprocess.run(
        ["say", "-v", VOICE, "-o", aiff, sentence],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["afconvert", aiff, m4a, "-f", "m4af", "-d", "aac"],
        check=True,
        capture_output=True,
    )
    os.remove(aiff)
    with open(m4a, "rb") as f:
        data = f.read()
    os.remove(m4a)
    return idx, base64.b64encode(data).decode()


def generate_all_audio(cards):
    audio = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(gen_audio, (i, c["sentence"])): i for i, c in enumerate(cards)}
        done = 0
        for f in as_completed(futures):
            idx, b64 = f.result()
            audio[idx] = b64
            done += 1  # noqa: SIM113
            if done % 40 == 0:
                print(f"  audio: {done}/{len(cards)} ({time.time() - t0:.0f}s)")
    print(f"  audio: {len(audio)}/{len(cards)} done ({time.time() - t0:.0f}s)")
    return audio


# ══════════════════════════════════════════════════════════════════════
# STEP 5-6: UPLOAD + CREATE + SYNC
# ══════════════════════════════════════════════════════════════════════


def upload_and_create(cards, audio, src_tag):
    created, skipped, failed = 0, 0, []

    t0 = time.time()
    for i, c in enumerate(cards):
        filename = media_filename(c["sentence"])
        try:
            ac("storeMediaFile", filename=filename, data=audio[i])
            ac(
                "addNote",
                note={
                    "deckName": DECK,
                    "modelName": MODEL,
                    "fields": {
                        "Sentence": c["sentence"],
                        "Word": c["word"],
                        "Pinyin": c["pinyin"],
                        "English": c["english"],
                        "Notes": c["notes"],
                        "Audio": f"[sound:{filename}]",
                    },
                    "tags": GLOBAL_TAGS + [src_tag] + c["tags"],
                },
            )
            created += 1
        except Exception as e:
            err = str(e)
            if "duplicate" in err.lower():
                skipped += 1
            else:
                failed.append((c["sentence"], err))
        if (created + skipped + len(failed)) % 40 == 0:
            print(
                f"  notes: {created + skipped + len(failed)}/{len(cards)} ({time.time() - t0:.0f}s)"
            )

    return created, skipped, failed


def sync():
    try:
        ac("sync")
        return "OK"
    except Exception as e:
        if "status 2" in str(e).lower() or "Status 2" in str(e):
            return "FULL_SYNC_NEEDED"
        return f"ERROR: {e}"


# ══════════════════════════════════════════════════════════════════════
# EXPORT KNOWN WORDS (for Migaku Memory)
# ══════════════════════════════════════════════════════════════════════


def _clean_word(raw):
    """Strip HTML, normalize 兒→裡, keep CJK only. Returns "" if not a vocab word."""
    if not raw:
        return ""
    s = re.sub(r"<[^>]+>", "", raw).strip()
    # Reject sentences (any punctuation)
    if any(p in s for p in "。？！，,.?!;:、；："):
        return ""
    for old, new in HANZI_CONVERSIONS.items():
        s = s.replace(old, new)
    cjk_only = "".join(c for c in s if is_cjk(c))
    return cjk_only


def export_known_words(output_path):
    all_words = set()
    for deck in KNOWN_WORDS_DECKS:
        try:
            note_ids = ac("findNotes", query=f'deck:"{deck}"')
        except Exception as e:
            print(f"  {deck}: ERROR {e}")
            continue
        if not note_ids:
            print(f"  {deck}: no notes")
            continue
        notes = ac("notesInfo", notes=note_ids)
        deck_words = set()
        for note in notes:
            fields = note.get("fields", {})
            word = ""
            for fname in KNOWN_WORDS_FIELDS:
                if fname in fields:
                    word = _clean_word(fields[fname].get("value", ""))
                    if word:
                        break
            if not word:
                # Fallback: first field whose cleaned value is a vocab word
                for fval in fields.values():
                    word = _clean_word(fval.get("value", ""))
                    if word:
                        break
            if word:
                deck_words.add(word)
        print(f"  {deck}: {len(notes)} notes → {len(deck_words)} unique words")
        all_words.update(deck_words)

    sorted_words = sorted(all_words)
    with open(output_path, "w") as f:
        f.write("\n".join(sorted_words) + "\n")
    print(f"\nWrote {len(sorted_words)} unique known words → {output_path}")
    print(
        "Import in Migaku: Memory → Settings → Known Words → Import (language: Chinese Traditional)"
    )


# ══════════════════════════════════════════════════════════════════════
# AUDIT COLLISIONS
# ══════════════════════════════════════════════════════════════════════


def audit_collisions():
    """Fetch all claude-tagged notes and report media files referenced by >1 sentence.

    Returns the number of colliding filenames (0 = clean). Used by --audit and by
    the post-import auto-verify step.
    """
    note_ids = ac("findNotes", query="tag:claude")
    notes = ac("notesInfo", notes=note_ids) if note_ids else []
    collisions = find_collisions(notes)
    if not collisions:
        print(f"AUDIT PASS: {len(notes)} claude notes, no filename collisions.")
        return 0
    print(f"AUDIT FAIL: {len(collisions)} audio file(s) map to >1 sentence:")
    for fn, sentences in sorted(collisions.items()):
        print(f"  {fn} -> {sentences}")
    return len(collisions)


# ══════════════════════════════════════════════════════════════════════
# REGENERATE AUDIO (for existing notes — e.g. after voice library update)
# ══════════════════════════════════════════════════════════════════════


def regenerate_audio_for_existing(deck=DECK, tag="claude"):
    query = f'deck:"{deck}" tag:{tag}'
    note_ids = ac("findNotes", query=query)
    if not note_ids:
        print(f"No notes found for: {query}")
        return
    notes = ac("notesInfo", notes=note_ids)

    targets = []
    skipped_no_audio = 0
    for note in notes:
        fields = note.get("fields", {})
        sentence = fields.get("Sentence", {}).get("value", "")
        audio_field = fields.get("Audio", {}).get("value", "")
        m = re.search(r"\[sound:([^\]]+)\]", audio_field)
        if not (sentence and m):
            skipped_no_audio += 1
            continue
        targets.append({"sentence": sentence, "filename": m.group(1)})

    print(
        f"Found {len(targets)} notes with audio to regenerate"
        f" ({skipped_no_audio} skipped — no Sentence or no [sound:] reference)"
    )
    if not targets:
        return

    print(f"Generating {len(targets)} audio files ({VOICE}, {WORKERS} workers)...")
    audio = generate_all_audio(targets)

    print("Overwriting media files in Anki...")
    t0 = time.time()
    uploaded, failed = 0, []
    for i, t in enumerate(targets):
        if i not in audio:
            failed.append((t["filename"], "audio gen missing"))
            continue
        try:
            ac("storeMediaFile", filename=t["filename"], data=audio[i])
            uploaded += 1
        except Exception as e:
            failed.append((t["filename"], str(e)))
        if (uploaded + len(failed)) % 40 == 0:
            print(f"  media: {uploaded + len(failed)}/{len(targets)} ({time.time() - t0:.0f}s)")

    sync_status = sync()
    print(f"\nDone: {uploaded} regenerated, {len(failed)} failed. Sync: {sync_status}")
    for fname, err in failed[:10]:
        print(f"  FAILED: {fname} — {err}")
    if sync_status == "FULL_SYNC_NEEDED":
        print("\n  Press Y in Anki desktop → choose 'Upload to AnkiWeb'")


# ══════════════════════════════════════════════════════════════════════
# REPAIR (migrate existing notes to content-hash audio, fixing collisions)
# ══════════════════════════════════════════════════════════════════════


def repair_audio(tag="claude"):
    """Migrate existing notes onto content-hash audio filenames.

    For every note whose audio is not already named after its Sentence's hash,
    regenerate the audio from the Sentence and re-point the note at a fresh
    claude_<sha1>.m4a file. This gives every note its own correct audio and
    eliminates the filename collisions that caused audio↔text mismatches.

    Scoped by tag across all decks (matching --audit), so claude notes that were
    moved into other decks are migrated too.
    """
    note_ids = ac("findNotes", query=f"tag:{tag}")
    if not note_ids:
        print(f"No notes found for: tag:{tag}")
        return
    notes = ac("notesInfo", notes=note_ids)

    targets = repair_targets(notes)
    if not targets:
        print(f"Nothing to repair: all {len(notes)} notes already use content-hash audio.")
        return
    print(f"Repairing {len(targets)}/{len(notes)} notes (migrating to content-hash audio)...")

    # Identical sentences hash to the same filename — generate each file only once.
    unique = {}
    for t in targets:
        unique.setdefault(t["filename"], t["sentence"])
    uniq_list = [{"sentence": s, "filename": fn} for fn, s in unique.items()]
    print(f"Generating {len(uniq_list)} audio files ({VOICE}, {WORKERS} workers)...")
    audio = generate_all_audio(uniq_list)
    fn_to_b64 = {uniq_list[i]["filename"]: b64 for i, b64 in audio.items()}

    # Store each new media file once.
    stored, store_failed = 0, []
    for fn, b64 in fn_to_b64.items():
        try:
            ac("storeMediaFile", filename=fn, data=b64)
            stored += 1
        except Exception as e:
            store_failed.append((fn, str(e)))

    # Re-point each note's Audio field at its content-hash file.
    print("Updating note Audio fields...")
    t0 = time.time()
    updated, update_failed = 0, []
    for t in targets:
        if t["filename"] not in fn_to_b64:
            update_failed.append((t["id"], "audio gen missing"))
            continue
        try:
            ac(
                "updateNoteFields",
                note={"id": t["id"], "fields": {"Audio": f"[sound:{t['filename']}]"}},
            )
            updated += 1
        except Exception as e:
            update_failed.append((t["id"], str(e)))
        if (updated + len(update_failed)) % 40 == 0:
            print(
                f"  notes: {updated + len(update_failed)}/{len(targets)} ({time.time() - t0:.0f}s)"
            )

    # Verify the collection is now clean, then sync.
    print("\nVerifying media integrity...")
    remaining = audit_collisions()

    sync_status = sync()
    print(
        f"\nDone: {updated} notes repaired, {stored} media files stored, "
        f"{len(update_failed)} update failures, {len(store_failed)} store failures. "
        f"Collisions remaining: {remaining}. Sync: {sync_status}"
    )
    for ident, err in (update_failed + store_failed)[:10]:
        print(f"  FAILED: {ident} — {err}")
    if sync_status == "FULL_SYNC_NEEDED":
        print("\n  Press Y in Anki desktop → choose 'Upload to AnkiWeb'")
    if remaining == 0:
        print(
            "\n  Old batch-named media files are now unreferenced. To reclaim space, run"
            "\n  Anki → Tools → Check Media → Delete Unused."
        )


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════


def main():
    # Export mode
    if len(sys.argv) >= 2 and sys.argv[1] == "--export-known-words":
        output = sys.argv[2] if len(sys.argv) >= 3 else KNOWN_WORDS_DEFAULT_OUTPUT
        ac("version")
        print(f"Exporting known words from {len(KNOWN_WORDS_DECKS)} deck(s)...")
        export_known_words(output)
        return

    # Audit mode
    if len(sys.argv) >= 2 and sys.argv[1] == "--audit":
        ac("version")
        sys.exit(1 if audit_collisions() else 0)

    # Regenerate-audio mode
    if len(sys.argv) >= 2 and sys.argv[1] == "--regenerate-audio":
        ac("version")
        regenerate_audio_for_existing()
        return

    # Repair mode: migrate existing notes to content-hash audio, fixing collisions
    if len(sys.argv) >= 2 and sys.argv[1] == "--repair":
        ac("version")
        repair_audio()
        return

    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <file.txt>")
        print(f"       python3 {sys.argv[0]} --export-known-words [output.txt]")
        print(f"       python3 {sys.argv[0]} --regenerate-audio")
        print(f"       python3 {sys.argv[0]} --audit")
        print(f"       python3 {sys.argv[0]} --repair")
        sys.exit(1)

    filepath = sys.argv[1]
    if not os.path.exists(filepath):
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    # Verify AnkiConnect is reachable
    ac("version")

    # Step 1: Parse
    entries = parse_file(filepath)
    print(f"Parsed: {len(entries)} entries")

    if not entries:
        print("No entries found. Check file format.")
        sys.exit(1)

    # Step 2: Convert
    converted = convert_er(entries)
    if converted:
        print(f"Converted: {converted} entries (兒→裡)")

    # Step 3: Classify
    cards = []
    for e in entries:
        word, tag = classify(e)
        cards.append(
            {
                "word": word,
                "sentence": e["hanzi"],
                "pinyin": e["pinyin"],
                "english": e["english"],
                "notes": "",
                "tags": [tag],
            }
        )
    vocab_count = sum(1 for c in cards if c["word"])
    print(f"Classified: {vocab_count} vocab, {len(cards) - vocab_count} sentences")

    # Step 4: Audio
    print(f"Generating {len(cards)} audio files ({VOICE}, {WORKERS} workers)...")
    audio = generate_all_audio(cards)

    # Step 5: Upload + Create
    src_tag = source_tag(filepath)
    print(f"Uploading to {DECK} as {src_tag}...")
    created, skipped, failed = upload_and_create(cards, audio, src_tag)

    # Step 6: Sync
    sync_status = sync()

    # Step 7: Summary
    print(
        f"\nDone: {created} created, {skipped} duplicates skipped, {len(failed)} failed. Sync: {sync_status}"
    )
    if failed:
        for sentence, err in failed[:10]:
            print(f"  FAILED: {sentence} — {err}")

    # Step 8: Auto-verify — confirm no filename now maps to two sentences.
    print("\nVerifying media integrity...")
    audit_collisions()

    if sync_status == "FULL_SYNC_NEEDED":
        print("\n  Press Y in Anki desktop → choose 'Upload to AnkiWeb'")


if __name__ == "__main__":
    main()
