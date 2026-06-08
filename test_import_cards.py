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
