# Collision-proof Card Creation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `import-cards.py` create Anki cards whose audio can never be overwritten by a later import, and add audit/verify safety nets.

**Architecture:** Replace the positional `claude_batch{BATCH}_{idx}.m4a` media naming with content-hash names (`claude_<sha1(sentence)[:10]>.m4a`), deleting the hand-edited `BATCH` global. Add three pure helper functions (`media_filename`, `source_tag`, `find_collisions`) that are unit-testable without Anki, then wire them into the import path, a new `--audit` CLI mode, and a post-import auto-verify step.

**Tech Stack:** Python 3.8+ stdlib only (`hashlib`, `re`, `os`), AnkiConnect HTTP API, pytest. Module is loaded in tests via `importlib` because the filename contains a hyphen.

---

## File Structure

- **Modify:** `import-cards.py` — add pure helpers, rewire `upload_and_create`, add `--audit` mode and auto-verify, delete `BATCH` global.
- **Create:** `test_import_cards.py` — pytest suite for the pure helpers (no Anki/macOS deps; CI-safe, mirrors `test_generate.py`'s `importlib` loader pattern).

The three new helpers each have one responsibility:
- `media_filename(sentence)` — deterministic content-hash filename.
- `source_tag(path)` — derive `src:<stem>` Anki tag from the input filename.
- `find_collisions(notes)` — given AnkiConnect `notesInfo` results, return filenames referenced by >1 distinct sentence. Shared by `--audit` and auto-verify.

---

## Task 1: `media_filename` — content-hash media names

**Files:**
- Modify: `import-cards.py` (add function near the other helpers, after `ac()`)
- Test: `test_import_cards.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_import_cards.py`:

```python
"""
Tests for import-cards.py pure helpers (no Anki/macOS deps; CI-safe).

The filename contains a hyphen, so it's loaded via importlib like test_generate.py.
"""

import importlib.util
import sys

_spec = importlib.util.spec_from_file_location("imp", "import-cards.py")
imp = importlib.util.module_from_spec(_spec)
sys.modules["imp"] = imp
_spec.loader.exec_module(imp)


class TestMediaFilename:
    def test_format_is_claude_hash_m4a(self):
        name = imp.media_filename("你好")
        assert name.startswith("claude_")
        assert name.endswith(".m4a")
        # claude_ (7) + 10 hex + .m4a (4) = 21 chars
        assert len(name) == 21

    def test_deterministic_same_text_same_name(self):
        assert imp.media_filename("我喜欢喝珍珠奶茶") == imp.media_filename("我喜欢喝珍珠奶茶")

    def test_different_text_different_name(self):
        assert imp.media_filename("吃") != imp.media_filename("我喜欢喝珍珠奶茶")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_import_cards.py::TestMediaFilename -v`
Expected: FAIL — `AttributeError: module 'imp' has no attribute 'media_filename'`

- [ ] **Step 3: Write minimal implementation**

Add `import hashlib` to the import block at the top of `import-cards.py`, then add this function after `ac()` (around line 97):

```python
def media_filename(sentence):
    """Content-addressed media name: identical text → identical file, never collides.

    Replaces the old positional claude_batch{BATCH}_{idx}.m4a scheme, whose
    uniqueness depended on hand-bumping a global counter.
    """
    digest = hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:10]
    return f"{FILENAME_PREFIX}_{digest}.m4a"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest test_import_cards.py::TestMediaFilename -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add import-cards.py test_import_cards.py
git commit -m "feat: add content-hash media_filename helper"
```

---

## Task 2: `source_tag` — derive import tag from filename

**Files:**
- Modify: `import-cards.py` (add function after `media_filename`)
- Test: `test_import_cards.py`

- [ ] **Step 1: Write the failing test**

Append to `test_import_cards.py`:

```python
class TestSourceTag:
    def test_basic_stem(self):
        assert imp.source_tag("hsk1-food.txt") == "src:hsk1-food"

    def test_strips_directory(self):
        assert imp.source_tag("/tmp/decks/hsk1-food.txt") == "src:hsk1-food"

    def test_spaces_become_hyphens(self):
        # Anki tags cannot contain spaces.
        assert imp.source_tag("my deck.txt") == "src:my-deck"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_import_cards.py::TestSourceTag -v`
Expected: FAIL — `AttributeError: module 'imp' has no attribute 'source_tag'`

- [ ] **Step 3: Write minimal implementation**

Add after `media_filename` in `import-cards.py`:

```python
def source_tag(path):
    """Derive a 'src:<stem>' Anki tag from the input filename.

    Replaces the old batch{BATCH} tag (also keyed off the hand-edited global).
    Whitespace is collapsed to hyphens because Anki tags cannot contain spaces.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    safe = re.sub(r"\s+", "-", stem.strip())
    return f"src:{safe}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest test_import_cards.py::TestSourceTag -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add import-cards.py test_import_cards.py
git commit -m "feat: add source_tag helper"
```

---

## Task 3: `find_collisions` — detect filename→multiple-sentence corruption

**Files:**
- Modify: `import-cards.py` (add function after `source_tag`)
- Test: `test_import_cards.py`

- [ ] **Step 1: Write the failing test**

Append to `test_import_cards.py`:

```python
def _note(sentence, filename):
    """Build a minimal notesInfo-shaped dict."""
    return {
        "fields": {
            "Sentence": {"value": sentence},
            "Audio": {"value": f"[sound:{filename}]"},
        }
    }


class TestFindCollisions:
    def test_no_collisions(self):
        notes = [_note("吃", "claude_aaa.m4a"), _note("喝", "claude_bbb.m4a")]
        assert imp.find_collisions(notes) == {}

    def test_one_filename_two_sentences(self):
        notes = [_note("吃", "claude_x.m4a"), _note("我喜欢喝珍珠奶茶", "claude_x.m4a")]
        result = imp.find_collisions(notes)
        assert set(result.keys()) == {"claude_x.m4a"}
        assert result["claude_x.m4a"] == ["吃", "我喜欢喝珍珠奶茶"]

    def test_same_sentence_same_file_is_not_a_collision(self):
        # Idempotent re-import: identical text reusing its own file is fine.
        notes = [_note("吃", "claude_x.m4a"), _note("吃", "claude_x.m4a")]
        assert imp.find_collisions(notes) == {}

    def test_note_without_audio_ref_is_ignored(self):
        notes = [{"fields": {"Sentence": {"value": "吃"}, "Audio": {"value": ""}}}]
        assert imp.find_collisions(notes) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_import_cards.py::TestFindCollisions -v`
Expected: FAIL — `AttributeError: module 'imp' has no attribute 'find_collisions'`

- [ ] **Step 3: Write minimal implementation**

Add after `source_tag` in `import-cards.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest test_import_cards.py::TestFindCollisions -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add import-cards.py test_import_cards.py
git commit -m "feat: add find_collisions audit helper"
```

---

## Task 4: Rewire `upload_and_create` to use content-hash names + source tag

**Files:**
- Modify: `import-cards.py:216-248` (`upload_and_create`) and its call site in `main()` (around line 439)

- [ ] **Step 1: Replace the function body**

Replace the entire `upload_and_create` function (currently lines 216-248) with this signature and body. The two changes: it now takes a `src_tag` argument instead of reading the `BATCH` global, and the filename comes from `media_filename(c["sentence"])`.

```python
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
```

- [ ] **Step 2: Update the call site in `main()`**

Find this block in `main()` (around line 438-440):

```python
    # Step 5: Upload + Create
    print(f"Uploading to {DECK} as batch{BATCH}...")
    created, skipped, failed = upload_and_create(cards, audio)
```

Replace with:

```python
    # Step 5: Upload + Create
    src_tag = source_tag(filepath)
    print(f"Uploading to {DECK} as {src_tag}...")
    created, skipped, failed = upload_and_create(cards, audio, src_tag)
```

- [ ] **Step 3: Verify the parser/helper tests still pass and the module imports**

Run: `pytest test_import_cards.py -v`
Expected: PASS (all helper tests; this confirms `import-cards.py` still imports cleanly after the edit)

- [ ] **Step 4: Commit**

```bash
git add import-cards.py
git commit -m "refactor: content-hash filenames and src tag in upload_and_create"
```

---

## Task 5: Add `--audit` CLI mode

**Files:**
- Modify: `import-cards.py` — add an `audit_collisions()` fetch wrapper near `regenerate_audio_for_existing` (around line 320) and a mode branch in `main()` (around line 378)

- [ ] **Step 1: Add the fetch wrapper**

Add this function in `import-cards.py` just above `regenerate_audio_for_existing` (around line 321):

```python
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
```

- [ ] **Step 2: Add the CLI branch**

In `main()`, after the `--export-known-words` block and before the `--regenerate-audio` block (around line 386), add:

```python
    # Audit mode
    if len(sys.argv) >= 2 and sys.argv[1] == "--audit":
        ac("version")
        sys.exit(1 if audit_collisions() else 0)
```

- [ ] **Step 3: Update the usage text**

In `main()`, find the usage block (around line 392-396) and add the `--audit` line:

```python
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <file.txt>")
        print(f"       python3 {sys.argv[0]} --export-known-words [output.txt]")
        print(f"       python3 {sys.argv[0]} --regenerate-audio")
        print(f"       python3 {sys.argv[0]} --audit")
        sys.exit(1)
```

- [ ] **Step 4: Run the audit against the live collection**

Run: `python3 import-cards.py --audit`
Expected: With Anki running, prints `AUDIT FAIL: 29 audio file(s) map to >1 sentence:` and lists them (the known pre-existing corruption), exit code 1. If Anki is not running, the existing `ac()` error handler prints the AnkiConnect-unreachable message and exits.

- [ ] **Step 5: Commit**

```bash
git add import-cards.py
git commit -m "feat: add --audit mode for filename collision detection"
```

---

## Task 6: Auto-verify after every import

**Files:**
- Modify: `import-cards.py` — `main()` import flow, after the sync step (around line 442-451)

- [ ] **Step 1: Add the verify call**

In `main()`, find the end of the import flow (after Step 6 sync, around line 442):

```python
# Step 6: Sync
sync_status = sync()

# Step 7: Summary
print(
    f"\nDone: {created} created, {skipped} duplicates skipped, {len(failed)} failed. Sync: {sync_status}"
)
if failed:
    for sentence, err in failed[:10]:
        print(f"  FAILED: {sentence} — {err}")
if sync_status == "FULL_SYNC_NEEDED":
    print("\n  Press Y in Anki desktop → choose 'Upload to AnkiWeb'")
```

Insert a verification step between the summary and the `FULL_SYNC_NEEDED` hint, so the block becomes:

```python
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
```

- [ ] **Step 2: Confirm the module still imports**

Run: `pytest test_import_cards.py -v`
Expected: PASS (all helper tests; confirms the edited file imports)

- [ ] **Step 3: Commit**

```bash
git add import-cards.py
git commit -m "feat: auto-verify media integrity after import"
```

---

## Task 7: Delete the `BATCH` global and update docs

**Files:**
- Modify: `import-cards.py` — remove the `BATCH` global (line 35) and refresh the module docstring/comments that reference it

- [ ] **Step 1: Remove the `BATCH` global**

Delete this line (line 35) and its trailing comment:

```python
BATCH = 7  # ← increment this each time you run a new file
```

Also update the comment on the `FILENAME_PREFIX` line (line 33), which currently reads:

```python
FILENAME_PREFIX = "claude"  # media: claude_batch{N}_{idx}.m4a
```

Change it to:

```python
FILENAME_PREFIX = "claude"  # media: claude_<sha1(sentence)[:10]>.m4a
```

- [ ] **Step 2: Update the module docstring**

In the top docstring (lines 5-10), add the `--audit` usage line so it reads:

```python
Usage:
    python3 import-cards.py <file.txt>                    # import a batch
    python3 import-cards.py --export-known-words [out]    # dump hanzi for Migaku
    python3 import-cards.py --regenerate-audio            # re-TTS all claude-tagged notes
    python3 import-cards.py --audit                       # report audio↔text mismatches
```

- [ ] **Step 3: Confirm nothing else references `BATCH`**

Run: `grep -n "BATCH" import-cards.py`
Expected: no output (every reference removed).

- [ ] **Step 4: Confirm the module still imports and tests pass**

Run: `pytest test_import_cards.py -v`
Expected: PASS (all helper tests).

- [ ] **Step 5: Lint**

Run: `ruff check import-cards.py test_import_cards.py && ruff format --check import-cards.py test_import_cards.py`
Expected: no errors. If `ruff format --check` reports a diff, run `ruff format import-cards.py test_import_cards.py` and re-stage.

- [ ] **Step 6: Commit**

```bash
git add import-cards.py
git commit -m "chore: remove BATCH global, document --audit and hash naming"
```

---

## Self-Review Notes

- **Spec §1 (content-hash naming):** Task 1 (`media_filename`) + Task 4 (wiring) + Task 7 (delete `BATCH`).
- **Spec §2 (idempotent re-runs):** Achieved by Task 1's determinism + existing `addNote` duplicate handling (unchanged); Task 3's `test_same_sentence_same_file_is_not_a_collision` documents the invariant.
- **Spec §3 (source tag):** Task 2 (`source_tag`) + Task 4 (call site).
- **Spec §4 (`--audit`):** Task 5.
- **Spec §5 (auto-verify):** Task 6.
- **Unchanged items** (parser, 儿→里, classify, parallel TTS, dedup, sync): not touched by any task.
- **Type consistency:** `media_filename`, `source_tag`, `find_collisions`, `audit_collisions` names used identically across tasks; `upload_and_create(cards, audio, src_tag)` signature matches its only call site in Task 4.
