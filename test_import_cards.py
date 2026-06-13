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


class TestSourceTag:
    def test_basic_stem(self):
        assert imp.source_tag("hsk1-food.txt") == "src:hsk1-food"

    def test_strips_directory(self):
        assert imp.source_tag("/tmp/decks/hsk1-food.txt") == "src:hsk1-food"

    def test_spaces_become_hyphens(self):
        # Anki tags cannot contain spaces.
        assert imp.source_tag("my deck.txt") == "src:my-deck"


class TestConvertEr:
    def test_traditional_er_to_li(self):
        entries = [{"hanzi": "你在哪兒？", "pinyin": "Nǐ zài nǎr?"}]
        count = imp.convert_er(entries)
        assert count == 1
        assert entries[0]["hanzi"] == "你在哪裡？"
        assert entries[0]["pinyin"] == "Nǐ zài nǎlǐ?"

    def test_no_er_unchanged(self):
        entries = [{"hanzi": "我想喝茶", "pinyin": "Wǒ xiǎng hē chá"}]
        assert imp.convert_er(entries) == 0
        assert entries[0]["hanzi"] == "我想喝茶"


class TestCleanWord:
    def test_traditional_word_passes_through(self):
        assert imp._clean_word("謝謝") == "謝謝"

    def test_traditional_er_normalized(self):
        assert imp._clean_word("這兒") == "這裡"

    def test_html_stripped(self):
        assert imp._clean_word("<b>朋友</b>") == "朋友"

    def test_sentence_rejected(self):
        assert imp._clean_word("我想喝茶。") == ""


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


def _note_with_id(note_id, sentence, filename):
    """notesInfo-shaped dict including a noteId and a [sound:] audio ref."""
    audio = f"[sound:{filename}]" if filename is not None else ""
    return {
        "noteId": note_id,
        "fields": {
            "Sentence": {"value": sentence},
            "Audio": {"value": audio},
        },
    }


class TestRepairTargets:
    def test_old_batch_name_is_a_target(self):
        # A note on a positional batch filename must migrate to its content hash.
        notes = [_note_with_id(1, "吃", "claude_batch5_001.m4a")]
        targets = imp.repair_targets(notes)
        assert targets == [{"id": 1, "sentence": "吃", "filename": imp.media_filename("吃")}]

    def test_already_hashed_is_not_a_target(self):
        # Idempotent: a note already on the correct content-hash name is left alone.
        notes = [_note_with_id(1, "吃", imp.media_filename("吃"))]
        assert imp.repair_targets(notes) == []

    def test_missing_audio_is_a_target(self):
        # A note with no [sound:] reference still needs its content-hash audio.
        notes = [_note_with_id(1, "吃", None)]
        targets = imp.repair_targets(notes)
        assert targets == [{"id": 1, "sentence": "吃", "filename": imp.media_filename("吃")}]

    def test_empty_sentence_is_skipped(self):
        notes = [_note_with_id(1, "", "claude_batch5_001.m4a")]
        assert imp.repair_targets(notes) == []
