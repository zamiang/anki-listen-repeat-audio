"""
Tests for revise-cards.py pure helpers (no Anki/macOS deps; CI-safe).

OpenCC-dependent tests are skipped when opencc isn't installed (it's the one
pip dependency in this repo, needed only for migration tooling).
"""

import importlib.util
import sys

import pytest

_spec = importlib.util.spec_from_file_location("rev", "revise-cards.py")
rev = importlib.util.module_from_spec(_spec)
sys.modules["rev"] = rev
_spec.loader.exec_module(rev)

imp = sys.modules["imp"]  # import-cards.py, loaded by revise-cards.py


def _note(note_id, sentence, pinyin="pin", english="eng", word="", notes="", tags=None):
    audio = f"[sound:{imp.media_filename(sentence)}]"
    return {
        "noteId": note_id,
        "tags": tags or [],
        "fields": {
            "Sentence": {"value": sentence},
            "Pinyin": {"value": pinyin},
            "English": {"value": english},
            "Word": {"value": word},
            "Notes": {"value": notes},
            "Audio": {"value": audio},
        },
    }


def _entry(note_id, sentence, pinyin="pin", english="eng", word="", notes="", **kw):
    e = {
        "noteId": note_id,
        "sentence": sentence,
        "pinyin": pinyin,
        "english": english,
        "word": word,
        "notes": notes,
        "rewrite": False,
        "add_tags": [],
        "remove_tags": [],
    }
    e.update(kw)
    return e


class TestEntryFromNote:
    def test_baseline_converts_sentence_word_notes_not_pinyin_english(self):
        note = _note(1, "我们这里", pinyin="Wǒmen zhèlǐ", english="we here", word="这里")
        e = rev.entry_from_note(note, lambda s: s.replace("这里", "這裡").replace("我们", "我們"))
        assert e["sentence"] == "我們這裡"
        assert e["word"] == "這裡"
        assert e["pinyin"] == "Wǒmen zhèlǐ"
        assert e["english"] == "we here"
        assert e["old"]["sentence"] == "我们这里"
        assert e["rewrite"] is False

    def test_preserves_note_id(self):
        e = rev.entry_from_note(_note(42, "吃"), lambda s: s)
        assert e["noteId"] == 42


class TestTargetFields:
    def test_audio_uses_content_hash_of_new_sentence(self):
        e = _entry(1, "我想喝茶")
        fields = rev.target_fields(e)
        assert fields["Audio"] == f"[sound:{imp.media_filename('我想喝茶')}]"
        assert fields["Sentence"] == "我想喝茶"


class TestNeedsUpdate:
    def test_matching_note_needs_nothing(self):
        # The idempotency predicate: live note already equals the target.
        e = _entry(1, "我想喝茶", pinyin="p", english="e")
        note = _note(1, "我想喝茶", pinyin="p", english="e")
        assert rev.needs_update(e, note) is False

    def test_changed_sentence_needs_update(self):
        e = _entry(1, "我想喝咖啡", pinyin="p", english="e")
        note = _note(1, "我想喝茶", pinyin="p", english="e")
        assert rev.needs_update(e, note) is True

    def test_stale_audio_filename_needs_update(self):
        # Same text but Audio still points at an old (non-hash) filename.
        e = _entry(1, "我想喝茶", pinyin="p", english="e")
        note = _note(1, "我想喝茶", pinyin="p", english="e")
        note["fields"]["Audio"]["value"] = "[sound:claude_batch3_07.m4a]"
        assert rev.needs_update(e, note) is True

    def test_pending_add_tag_needs_update(self):
        e = _entry(1, "我想喝茶", pinyin="p", english="e", add_tags=["rewritten"])
        note = _note(1, "我想喝茶", pinyin="p", english="e", tags=["claude"])
        assert rev.needs_update(e, note) is True
        note["tags"] = ["claude", "rewritten"]
        assert rev.needs_update(e, note) is False


class TestValidateEntries:
    def test_valid_file_passes(self):
        entries = [_entry(1, "我想喝茶。"), _entry(None, "你要不要進來坐？")]
        assert rev.validate_entries(entries) == []

    def test_missing_keys_flagged(self):
        assert rev.validate_entries([{"noteId": 1}]) != []

    def test_empty_sentence_flagged(self):
        assert rev.validate_entries([_entry(1, "")]) != []

    def test_no_cjk_flagged(self):
        assert rev.validate_entries([_entry(1, "hello")]) != []

    def test_word_must_be_substring(self):
        ok = rev.validate_entries([_entry(1, "我想喝茶", word="茶")])
        bad = rev.validate_entries([_entry(1, "我想喝茶", word="咖啡")])
        assert ok == [] and bad != []

    def test_duplicate_sentences_flagged(self):
        entries = [_entry(1, "我想喝茶"), _entry(2, "我想喝茶")]
        assert any("duplicate sentence" in p for p in rev.validate_entries(entries))

    def test_duplicate_note_ids_flagged(self):
        entries = [_entry(1, "我想喝茶"), _entry(1, "我想喝咖啡")]
        assert any("duplicate noteId" in p for p in rev.validate_entries(entries))

    def test_new_cards_null_id_not_duplicate(self):
        entries = [_entry(None, "我想喝茶"), _entry(None, "我想喝咖啡")]
        assert rev.validate_entries(entries) == []


class TestValidateWithOpenCC:
    def test_simplified_residue_flagged(self):
        opencc = pytest.importorskip("opencc")
        cc = opencc.OpenCC("s2twp")
        entries = [_entry(1, "我们这里有奶茶")]  # simplified — not s2twp-stable
        assert any("s2twp" in p for p in rev.validate_entries(entries, convert=cc.convert))

    def test_traditional_taiwan_text_stable(self):
        opencc = pytest.importorskip("opencc")
        cc = opencc.OpenCC("s2twp")
        entries = [_entry(1, "我們這裡有好喝的奶茶")]
        assert rev.validate_entries(entries, convert=cc.convert) == []


class TestJsonlRoundTrip:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "rev.jsonl"
        entries = [_entry(1, "我想喝茶"), _entry(None, "你好")]
        rev.write_jsonl(str(path), entries)
        assert rev.read_jsonl(str(path)) == entries

    def test_blank_lines_skipped(self, tmp_path):
        path = tmp_path / "rev.jsonl"
        path.write_text('{"a": 1}\n\n{"b": 2}\n')
        assert rev.read_jsonl(str(path)) == [{"a": 1}, {"b": 2}]
