#!/usr/bin/env python3
"""
Bulk revision tool for the claude-tagged Anki notes: simplified→traditional
migration plus in-place sentence rewrites, applied from a reviewable JSONL file.

Workflow:
    python3 revise-cards.py backup [--out pre-trad-backup.apkg]
    python3 revise-cards.py export [--out revision.jsonl]
        Dump every tag:claude note as one JSONL line, with an OpenCC s2twp
        (traditional, Taiwan standard + phrases) baseline conversion applied
        to Sentence/Word/Notes. Review and edit the file (rewrite sentences,
        append new cards with "noteId": null), then:
    python3 revise-cards.py validate revision.jsonl
    python3 revise-cards.py apply revision.jsonl [--dry-run] [--limit N]
        Idempotent: notes whose fields already match the target are skipped,
        so an interrupted apply can simply be re-run.

Requires: Anki running with AnkiConnect (port 8765), macOS say + afconvert.
The export/validate subcommands also require `pip3 install opencc` — the only
pip dependency in this repo, used for migration tooling only.
"""

import argparse
import importlib.util
import json
import os
import sys

# import-cards.py has a hyphen in its name, so load it via importlib
# (same pattern as the test suites) and reuse its AnkiConnect + audio helpers.
_spec = importlib.util.spec_from_file_location(
    "imp", os.path.join(os.path.dirname(os.path.abspath(__file__)), "import-cards.py")
)
imp = importlib.util.module_from_spec(_spec)
sys.modules["imp"] = imp
_spec.loader.exec_module(imp)

QUERY = "tag:claude"
FIELD_KEYS = ["sentence", "pinyin", "english", "word", "notes"]


def _opencc():
    try:
        from opencc import OpenCC
    except ImportError:
        sys.exit("ERROR: opencc not installed. Run: pip3 install opencc")
    return OpenCC("s2twp")


# ══════════════════════════════════════════════════════════════════════
# PURE HELPERS (unit-tested in test_revise_cards.py)
# ══════════════════════════════════════════════════════════════════════


def entry_from_note(note, convert):
    """Build a revision-file entry from a notesInfo result.

    `convert` is a str→str conversion function (OpenCC s2twp). The converted
    text is the editable baseline; the original fields are preserved under
    "old" for review diffs.
    """
    f = note["fields"]
    old = {
        "sentence": f.get("Sentence", {}).get("value", "").strip(),
        "pinyin": f.get("Pinyin", {}).get("value", "").strip(),
        "english": f.get("English", {}).get("value", "").strip(),
        "word": f.get("Word", {}).get("value", "").strip(),
        "notes": f.get("Notes", {}).get("value", "").strip(),
    }
    return {
        "noteId": note["noteId"],
        "old": old,
        "sentence": convert(old["sentence"]),
        "pinyin": old["pinyin"],
        "english": old["english"],
        "word": convert(old["word"]),
        "notes": convert(old["notes"]),
        "rewrite": False,
        "add_tags": [],
        "remove_tags": [],
    }


def target_fields(entry):
    """The Anki field values an entry should end up with (Audio included)."""
    return {
        "Sentence": entry["sentence"],
        "Pinyin": entry["pinyin"],
        "English": entry["english"],
        "Word": entry["word"],
        "Notes": entry["notes"],
        "Audio": f"[sound:{imp.media_filename(entry['sentence'])}]",
    }


def needs_update(entry, note):
    """True if the live note's fields differ from the entry's targets.

    This is the idempotency predicate: after a successful apply it returns
    False for every entry, so re-running apply is a no-op.
    """
    current = {k: v.get("value", "").strip() for k, v in note["fields"].items()}
    for field, want in target_fields(entry).items():
        if current.get(field, "") != want:
            return True
    tags = set(note.get("tags", []))
    if any(t not in tags for t in entry.get("add_tags", [])):
        return True
    return any(t in tags for t in entry.get("remove_tags", []))


def validate_entries(entries, convert=None):
    """Return a list of "line N: problem" strings (empty = valid).

    `convert` (s2twp) enables the fixed-point check that catches simplified
    characters accidentally left in or introduced during the rewrite pass.
    """
    problems = []
    seen_ids = {}
    seen_sentences = {}
    for n, e in enumerate(entries, 1):

        def bad(msg, n=n):
            problems.append(f"line {n}: {msg}")

        if not isinstance(e, dict):
            bad("not a JSON object")
            continue
        missing = [k for k in ["noteId", "sentence", "pinyin", "english"] if k not in e]
        if missing:
            bad(f"missing keys: {missing}")
            continue
        sentence = e["sentence"]
        if not sentence or not any(imp.is_cjk(c) for c in sentence):
            bad(f"sentence empty or has no CJK: {sentence!r}")
        if not e["pinyin"].strip():
            bad("empty pinyin")
        if not e["english"].strip():
            bad("empty english")
        if e.get("word") and e["word"] not in sentence:
            bad(f"word {e['word']!r} not in sentence {sentence!r}")
        if convert and convert(sentence) != sentence:
            bad(f"sentence is not s2twp-stable (simplified residue?): {sentence!r}")
        if sentence in seen_sentences:
            bad(f"duplicate sentence (also line {seen_sentences[sentence]}): {sentence!r}")
        else:
            seen_sentences[sentence] = n
        nid = e["noteId"]
        if nid is not None:
            if nid in seen_ids:
                bad(f"duplicate noteId {nid} (also line {seen_ids[nid]})")
            else:
                seen_ids[nid] = n
    return problems


def read_jsonl(path):
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def write_jsonl(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ══════════════════════════════════════════════════════════════════════
# SUBCOMMANDS
# ══════════════════════════════════════════════════════════════════════


def cmd_backup(args):
    path = os.path.abspath(args.out)
    imp.ac("exportPackage", deck="HSK 1", path=path, includeSched=True)
    print(f"Backup written: {path}")


def cmd_export(args):
    cc = _opencc()
    note_ids = imp.ac("findNotes", query=QUERY)
    if not note_ids:
        sys.exit(f"No notes found for: {QUERY}")
    notes = imp.ac("notesInfo", notes=note_ids)
    entries = [entry_from_note(n, cc.convert) for n in notes]
    changed = sum(1 for e in entries if e["sentence"] != e["old"]["sentence"])
    write_jsonl(args.out, entries)
    print(f"Exported {len(entries)} notes → {args.out}")
    print(f"  s2twp baseline changed the sentence on {changed} of them")
    print("Next: review/rewrite the file, then `revise-cards.py validate` and `apply`.")


def cmd_validate(args):
    entries = read_jsonl(args.file)
    convert = _opencc().convert
    problems = validate_entries(entries, convert=convert)
    existing = sum(1 for e in entries if e.get("noteId") is not None)
    rewrites = sum(1 for e in entries if e.get("rewrite"))
    print(
        f"{len(entries)} entries: {existing} existing notes "
        f"({rewrites} rewritten), {len(entries) - existing} new cards"
    )
    if problems:
        print(f"INVALID — {len(problems)} problem(s):")
        for p in problems:
            print(f"  {p}")
        sys.exit(1)
    print("VALID")


def cmd_apply(args):
    entries = read_jsonl(args.file)
    problems = validate_entries(entries)
    if problems:
        sys.exit(f"File fails validation ({len(problems)} problems) — run `validate` first.")
    if args.limit:
        entries = entries[: args.limit]

    existing = [e for e in entries if e.get("noteId") is not None]
    new = [e for e in entries if e.get("noteId") is None]

    # Fetch live state for all existing notes; skip ones already matching.
    notes_by_id = {}
    if existing:
        infos = imp.ac("notesInfo", notes=[e["noteId"] for e in existing])
        notes_by_id = {n["noteId"]: n for n in infos if n}
    missing = [e["noteId"] for e in existing if e["noteId"] not in notes_by_id]
    if missing:
        sys.exit(f"ERROR: {len(missing)} noteIds not found in Anki (first: {missing[:5]})")

    todo = [e for e in existing if needs_update(e, notes_by_id[e["noteId"]])]
    skipped = len(existing) - len(todo)

    print(
        f"{len(existing)} existing notes: {len(todo)} to update, {skipped} already match. "
        f"{len(new)} new cards to add."
    )
    if args.dry_run:
        rewrites = sum(1 for e in todo if e.get("rewrite"))
        print(f"DRY RUN — would update {len(todo)} ({rewrites} rewrites), add {len(new)}.")
        for e in todo[:10]:
            print(f"  {e['noteId']}: {e['old']['sentence']} → {e['sentence']}")
        return
    if not todo and not new:
        print("Nothing to do.")
        return

    # Generate audio once per unique target filename (updates + new cards).
    needed = {}
    for e in todo + new:
        needed.setdefault(imp.media_filename(e["sentence"]), e["sentence"])
    uniq = [{"sentence": s, "filename": fn} for fn, s in needed.items()]
    print(f"Generating {len(uniq)} audio files ({imp.VOICE}, {imp.WORKERS} workers)...")
    audio = imp.generate_all_audio(uniq)
    fn_to_b64 = {uniq[i]["filename"]: b64 for i, b64 in audio.items()}

    stored, failed = 0, []
    for fn, b64 in fn_to_b64.items():
        try:
            imp.ac("storeMediaFile", filename=fn, data=b64)
            stored += 1
        except Exception as e:
            failed.append((fn, str(e)))
    print(f"Stored {stored} media files ({len(failed)} failed).")

    updated, update_failed = 0, []
    for e in todo:
        fn = imp.media_filename(e["sentence"])
        if fn not in fn_to_b64:
            update_failed.append((e["noteId"], "audio gen missing"))
            continue
        try:
            imp.ac("updateNoteFields", note={"id": e["noteId"], "fields": target_fields(e)})
            if e.get("add_tags"):
                imp.ac("addTags", notes=[e["noteId"]], tags=" ".join(e["add_tags"]))
            if e.get("remove_tags"):
                imp.ac("removeTags", notes=[e["noteId"]], tags=" ".join(e["remove_tags"]))
            updated += 1
        except Exception as err:
            update_failed.append((e["noteId"], str(err)))
        if updated % 40 == 0 and updated:
            print(f"  notes: {updated}/{len(todo)}")

    created, dup_skipped, create_failed = 0, 0, []
    for e in new:
        fn = imp.media_filename(e["sentence"])
        try:
            imp.ac(
                "addNote",
                note={
                    "deckName": imp.DECK,
                    "modelName": imp.MODEL,
                    "fields": target_fields(e),
                    "tags": imp.GLOBAL_TAGS + e.get("tags", []),
                },
            )
            created += 1
        except Exception as err:
            if "duplicate" in str(err).lower():
                dup_skipped += 1
            else:
                create_failed.append((e["sentence"], str(err)))

    print("\nVerifying media integrity...")
    collisions = imp.audit_collisions()
    sync_status = imp.sync()
    print(
        f"\nDone: {updated} updated, {created} created, {dup_skipped} duplicates skipped, "
        f"{len(update_failed) + len(create_failed)} failed. "
        f"Collisions: {collisions}. Sync: {sync_status}"
    )
    for ident, err in (update_failed + create_failed)[:10]:
        print(f"  FAILED: {ident} — {err}")
    if sync_status == "FULL_SYNC_NEEDED":
        print("\n  Press Y in Anki desktop → choose 'Upload to AnkiWeb'")
    print(
        "\n  Old media files are now unreferenced. To reclaim space, run"
        "\n  Anki → Tools → Check Media → Delete Unused."
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="export the HSK 1 deck as an .apkg")
    b.add_argument("--out", default="pre-trad-backup.apkg")
    b.set_defaults(func=cmd_backup)

    e = sub.add_parser("export", help="dump tag:claude notes to JSONL with s2twp baseline")
    e.add_argument("--out", default="revision.jsonl")
    e.set_defaults(func=cmd_export)

    v = sub.add_parser("validate", help="check a revision file (no Anki writes)")
    v.add_argument("file")
    v.set_defaults(func=cmd_validate)

    a = sub.add_parser("apply", help="apply a revision file to Anki with new audio")
    a.add_argument("file")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--limit", type=int, default=0)
    a.set_defaults(func=cmd_apply)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
