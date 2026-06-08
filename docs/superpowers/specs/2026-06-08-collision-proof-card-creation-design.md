# Collision-proof card creation in `import-cards.py`

**Date:** 2026-06-08
**Status:** Approved (design)

## Problem

The `claude` Anki collection (645 notes, all `tag:claude`) contains cards whose
audio does not match their text. An audit of the live collection found **29 audio
filenames each referenced by two different sentences**, for example:

- `claude_batch5_001.m4a` → both `吃` and `我喜欢喝珍珠奶茶`
- `claude_batch5_004.m4a` → both `了` and `今天晚上吃鸡肉`

In each colliding pair, only one note plays correct audio; the other plays a
different sentence.

### Root cause

In `import-cards.py`, the media filename is:

```
claude_batch{BATCH}_{idx+1:03d}.m4a
```

where:

- `BATCH` is a **hand-edited global constant** (one line in the file), and
- `idx` **resets to `001` on every run**.

Filename uniqueness therefore depends entirely on the operator remembering to bump
`BATCH` before each import. The collection data shows this failed at least once: 30
notes *tagged* `batch6` actually carry `claude_batch5_*` filenames. They overwrote
the first 30 files of the real batch-5 import via `storeMediaFile` (which silently
overwrites), leaving the earlier notes pointing at audio that says something else.

The failure is silent — no error is raised — so a corrupted import looks identical
to a clean one.

## Scope

**Forward-looking creation tool only.** This design makes future imports
collision-proof. It does **not** repair the 29 already-broken cards; that is a
separate task, enabled by the `--audit` foundation below, to be done later on
request.

## Approach

Modify `import-cards.py` in place rather than introduce a new file — it is the
creation tool, and the bug lives in its naming logic. `generate-practice-audio.py`
is unaffected: it reads Anki *fields*, not media filenames.

### 1. Content-hash media naming (core fix)

Replace `claude_batch{BATCH}_{idx+1:03d}.m4a` with:

```
claude_<sha1(sentence)[:10]>.m4a
```

- The name is derived purely from the sentence text. Identical text always yields
  the same file (automatic dedup); different text can never collide.
- **Delete the `BATCH` global and the per-run `idx` counter entirely.** Nothing is
  hand-edited; nothing positional can drift.
- Hash input is the exact `Sentence` field value (post `儿→里` conversion, so the
  stored text and the audio stay consistent).

### 2. Idempotent re-runs

Re-importing the same file becomes safe:

- `storeMediaFile` rewrites identical bytes to the same name (no-op).
- `addNote` still hits Anki's duplicate guard and is skipped.

Running an import twice creates nothing new and corrupts nothing.

### 3. Source tag replaces the batch tag

The current `batch{BATCH}` tag is also keyed off the global. Replace it with a
source tag derived from the input filename stem, e.g. `src:hsk1-food` from
`hsk1-food.txt`, preserving a "which import added this" handle without a counter.

### 4. `--audit` command

Standalone subcommand. Scans all `tag:claude` notes and reports any media filename
referenced by more than one distinct sentence — the exact check used to diagnose
this bug. Exits non-zero when collisions exist. This is also the foundation for a
later repair of the existing 29 broken cards.

### 5. Auto-verify after every import

After creating notes, read back the just-created notes and assert each new filename
maps to exactly one sentence. Print a clear PASS/FAIL summary so a bad import can
never again pass silently.

## Unchanged

- The 4-line block parser (`id / english / pinyin / hanzi`)
- `儿→里` hanzi and pinyin conversion
- Word-vs-sentence and theme-tag classification
- Parallel TTS generation (`say` → `afconvert`)
- `addNote` duplicate handling and `sync`

## Out of scope

- Repairing the 29 existing broken cards (separate task; uses `--audit`)
- Any change to `generate-practice-audio.py`
- Changes to the input file format
