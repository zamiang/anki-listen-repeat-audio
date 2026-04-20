"""
Tests for generate-practice-audio.py

Red-green style: each test verifies actual behavior, not assumed correctness.
Audio tests require ffmpeg (skipped if not available).
TTS tests require macOS say (skipped on other platforms).
"""

import importlib.util
import os
import platform
import subprocess
import sys
import tempfile

import pytest

# ── Load the module under test ──────────────────────────────────────
# The filename contains hyphens, so a normal import won't work.

_spec = importlib.util.spec_from_file_location("gen", "generate-practice-audio.py")
gen = importlib.util.module_from_spec(_spec)
sys.modules["gen"] = gen
_spec.loader.exec_module(gen)


# ── Helpers ─────────────────────────────────────────────────────────


def has_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def has_say():
    return platform.system() == "Darwin"


def write_vocab_file(text):
    """Write text to a temp file and return the path."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(text)
        return f.name


skip_no_ffmpeg = pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not installed")
skip_no_say = pytest.mark.skipif(not has_say(), reason="macOS say not available")


# ═══════════════════════════════════════════════════════════════════
# FILE PARSER TESTS
# ═══════════════════════════════════════════════════════════════════


class TestParseFile:
    def test_basic_two_entries(self):
        path = write_vocab_file("0001\nHello\nnǐ hǎo\n你好\n\n0002\nThank you\nxiè xiè\n谢谢\n")
        try:
            entries = gen.parse_file(path)
            assert len(entries) == 2
            assert entries[0]["hanzi"] == "你好"
            assert entries[0]["english"] == "Hello"
            assert entries[0]["pinyin"] == "nǐ hǎo"
            assert entries[1]["hanzi"] == "谢谢"
            assert entries[1]["english"] == "Thank you"
            assert entries[1]["pinyin"] == "xiè xiè"
        finally:
            os.remove(path)

    def test_single_entry(self):
        path = write_vocab_file("0001\nHello\nnǐ hǎo\n你好\n")
        try:
            entries = gen.parse_file(path)
            assert len(entries) == 1
            assert entries[0]["english"] == "Hello"
        finally:
            os.remove(path)

    def test_empty_file(self):
        path = write_vocab_file("")
        try:
            entries = gen.parse_file(path)
            assert entries == []
        finally:
            os.remove(path)

    def test_malformed_entry_missing_fields(self):
        """Entry with only 3 lines (missing hanzi) should be skipped."""
        path = write_vocab_file("0001\nHello\nnǐ hǎo\n")
        try:
            entries = gen.parse_file(path)
            assert entries == []
        finally:
            os.remove(path)

    def test_extra_whitespace_stripped(self):
        path = write_vocab_file("0001\n  Hello  \n  nǐ hǎo  \n  你好  \n")
        try:
            entries = gen.parse_file(path)
            assert len(entries) == 1
            assert entries[0]["english"] == "Hello"
            assert entries[0]["hanzi"] == "你好"
        finally:
            os.remove(path)

    def test_non_id_text_ignored(self):
        """Text that doesn't start with a 4-digit ID is skipped."""
        path = write_vocab_file("Some header text\n\n0001\nHello\nnǐ hǎo\n你好\n")
        try:
            entries = gen.parse_file(path)
            assert len(entries) == 1
            assert entries[0]["english"] == "Hello"
        finally:
            os.remove(path)

    def test_five_digit_id_not_matched(self):
        """IDs must be exactly 4 digits."""
        path = write_vocab_file("00001\nHello\nnǐ hǎo\n你好\n")
        try:
            entries = gen.parse_file(path)
            assert entries == []
        finally:
            os.remove(path)

    def test_multiple_entries_preserve_order(self):
        text = "0001\nFirst\npīnyīn1\n第一\n\n0002\nSecond\npīnyīn2\n第二\n\n0003\nThird\npīnyīn3\n第三\n"
        path = write_vocab_file(text)
        try:
            entries = gen.parse_file(path)
            assert len(entries) == 3
            assert [e["english"] for e in entries] == ["First", "Second", "Third"]
        finally:
            os.remove(path)

    def test_field_ordering(self):
        """Verify the parser maps line positions correctly: ID, English, Pinyin, Hanzi."""
        path = write_vocab_file("0001\nENGLISH\nPINYIN\nHANZI\n")
        try:
            entries = gen.parse_file(path)
            assert entries[0]["english"] == "ENGLISH"
            assert entries[0]["pinyin"] == "PINYIN"
            assert entries[0]["hanzi"] == "HANZI"
        finally:
            os.remove(path)


# ═══════════════════════════════════════════════════════════════════
# SILENCE GENERATION TESTS (require ffmpeg)
# ═══════════════════════════════════════════════════════════════════


class TestGenerateSilence:
    @skip_no_ffmpeg
    def test_silence_file_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "silence.wav")
            gen.generate_silence(3, out)
            assert os.path.exists(out)
            assert os.path.getsize(out) > 0

    @skip_no_ffmpeg
    def test_silence_duration_matches_request(self):
        """The generated silence should be within 0.1s of the requested duration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "silence.wav")
            gen.generate_silence(3, out)
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    out,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            duration = float(result.stdout.strip())
            assert abs(duration - 3.0) < 0.1, f"Expected ~3.0s, got {duration}s"

    @skip_no_ffmpeg
    def test_silence_sample_rate_matches_tts(self):
        """Silence must be generated at TTS_SAMPLE_RATE to avoid concat distortion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "silence.wav")
            gen.generate_silence(1, out)
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=sample_rate",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    out,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            sample_rate = int(result.stdout.strip())
            assert sample_rate == gen.TTS_SAMPLE_RATE, (
                f"Silence sample rate {sample_rate} != TTS_SAMPLE_RATE {gen.TTS_SAMPLE_RATE}"
            )

    @skip_no_ffmpeg
    def test_silence_is_mono(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "silence.wav")
            gen.generate_silence(1, out)
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=channels",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    out,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            channels = int(result.stdout.strip())
            assert channels == 1


# ═══════════════════════════════════════════════════════════════════
# AUDIO CONCAT TESTS (require ffmpeg)
# ═══════════════════════════════════════════════════════════════════


class TestConcatAudio:
    @skip_no_ffmpeg
    def test_concat_two_silence_files(self):
        """Concatenating two 1s silence files should produce ~2s output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            s1 = os.path.join(tmpdir, "s1.wav")
            s2 = os.path.join(tmpdir, "s2.wav")
            out = os.path.join(tmpdir, "out.m4a")
            gen.generate_silence(1, s1)
            gen.generate_silence(1, s2)
            gen.concat_audio([s1, s2], out)
            assert os.path.exists(out)
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    out,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            duration = float(result.stdout.strip())
            assert abs(duration - 2.0) < 0.2, f"Expected ~2.0s, got {duration}s"

    @skip_no_ffmpeg
    def test_concat_output_is_aac(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s1 = os.path.join(tmpdir, "s1.wav")
            out = os.path.join(tmpdir, "out.m4a")
            gen.generate_silence(1, s1)
            gen.concat_audio([s1], out)
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    out,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.strip() == "aac"

    @skip_no_ffmpeg
    def test_concat_has_faststart(self):
        """Output should have moov atom before mdat (faststart)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            s1 = os.path.join(tmpdir, "s1.wav")
            out = os.path.join(tmpdir, "out.m4a")
            gen.generate_silence(1, s1)
            gen.concat_audio([s1], out)
            # ffprobe trace shows atom order — moov should appear before mdat
            result = subprocess.run(
                ["ffprobe", "-v", "trace", "-i", out],
                capture_output=True,
                text=True,
            )
            trace = result.stderr
            moov_pos = trace.find("type:'moov'")
            mdat_pos = trace.find("type:'mdat'")
            assert moov_pos != -1, "moov atom not found"
            assert mdat_pos != -1, "mdat atom not found"
            assert moov_pos < mdat_pos, "moov should come before mdat (faststart)"

    @skip_no_ffmpeg
    def test_concat_cleans_up_listfile(self):
        """The temporary concat list file should be removed after encoding."""
        with tempfile.TemporaryDirectory() as tmpdir:
            s1 = os.path.join(tmpdir, "s1.wav")
            out = os.path.join(tmpdir, "out.m4a")
            gen.generate_silence(1, s1)

            # Count .txt files before and after
            txt_before = len([f for f in os.listdir(tempfile.gettempdir()) if f.endswith(".txt")])
            gen.concat_audio([s1], out)
            txt_after = len([f for f in os.listdir(tempfile.gettempdir()) if f.endswith(".txt")])
            assert txt_after <= txt_before


# ═══════════════════════════════════════════════════════════════════
# TTS TESTS (require macOS say + ffmpeg)
# ═══════════════════════════════════════════════════════════════════


class TestSayToWav:
    @skip_no_say
    @skip_no_ffmpeg
    def test_generates_wav_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "test.wav")
            gen.say_to_wav("hello", "Samantha", out)
            assert os.path.exists(out)
            assert os.path.getsize(out) > 100

    @skip_no_say
    @skip_no_ffmpeg
    def test_cleans_up_aiff_intermediate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "test.wav")
            gen.say_to_wav("hello", "Samantha", out)
            aiff = out + ".aiff"
            assert not os.path.exists(aiff), "AIFF intermediate should be deleted"

    @skip_no_say
    @skip_no_ffmpeg
    def test_output_is_valid_audio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "test.wav")
            gen.say_to_wav("hello", "Samantha", out)
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    out,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.strip() == "audio"


# ═══════════════════════════════════════════════════════════════════
# BUILD TRACK TESTS (require macOS say + ffmpeg)
# ═══════════════════════════════════════════════════════════════════


class TestBuildTrack:
    ENTRY = {"hanzi": "你好", "english": "Hello", "pinyin": "nǐ hǎo"}

    @skip_no_say
    @skip_no_ffmpeg
    def test_recognition_order_is_zh_silence_en(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parts, temps = gen.build_track(0, self.ENTRY, tmpdir, "recognition", 3)
            assert len(parts) == 3
            assert "_zh.wav" in parts[0]
            assert "_silence.wav" in parts[1]
            assert "_en.wav" in parts[2]

    @skip_no_say
    @skip_no_ffmpeg
    def test_production_order_is_en_silence_zh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parts, temps = gen.build_track(0, self.ENTRY, tmpdir, "production", 3)
            assert len(parts) == 3
            assert "_en.wav" in parts[0]
            assert "_silence.wav" in parts[1]
            assert "_zh.wav" in parts[2]

    @skip_no_say
    @skip_no_ffmpeg
    def test_all_parts_are_real_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parts, temps = gen.build_track(0, self.ENTRY, tmpdir, "recognition", 3)
            for p in parts:
                assert os.path.exists(p), f"Part file missing: {p}"
                assert os.path.getsize(p) > 0, f"Part file empty: {p}"

    @skip_no_say
    @skip_no_ffmpeg
    def test_temps_list_matches_parts(self):
        """temps should contain all generated files for cleanup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            parts, temps = gen.build_track(0, self.ENTRY, tmpdir, "recognition", 3)
            assert set(temps) == {parts[0], parts[1], parts[2]}


# ═══════════════════════════════════════════════════════════════════
# BUILD SINGLE TRACK TESTS (require macOS say + ffmpeg)
# ═══════════════════════════════════════════════════════════════════


class TestBuildSingleTrack:
    ENTRY = {"hanzi": "你好", "english": "Hello", "pinyin": "nǐ hǎo"}

    @skip_no_say
    @skip_no_ffmpeg
    def test_batch_mode_returns_wav_parts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idx, result = gen.build_single_track((0, self.ENTRY, tmpdir, "recognition", 3, True))
            assert idx == 0
            assert isinstance(result, list)
            assert len(result) == 3
            assert all(p.endswith(".wav") for p in result)

    @skip_no_say
    @skip_no_ffmpeg
    def test_individual_mode_returns_m4a_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            idx, result = gen.build_single_track((0, self.ENTRY, tmpdir, "recognition", 3, False))
            assert idx == 0
            assert isinstance(result, str)
            assert result.endswith(".m4a")
            assert os.path.exists(result)


# ═══════════════════════════════════════════════════════════════════
# END-TO-END INTEGRATION TEST (require macOS say + ffmpeg)
# ═══════════════════════════════════════════════════════════════════


class TestEndToEnd:
    @skip_no_say
    @skip_no_ffmpeg
    def test_file_source_individual_mode(self):
        """Full pipeline: text file → individual m4a files."""
        text = "0001\nHello\nnǐ hǎo\n你好\n\n0002\nThank you\nxiè xiè\n谢谢\n"
        vocab_path = write_vocab_file(text)
        try:
            with tempfile.TemporaryDirectory() as outdir:
                args = [
                    "generate-practice-audio.py",
                    "--source",
                    "file",
                    "--file",
                    vocab_path,
                    "--mode",
                    "recognition",
                    "--output",
                    outdir,
                ]
                # Run as subprocess to test CLI integration
                result = subprocess.run(
                    ["python3"] + args,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                assert result.returncode == 0, f"Script failed:\n{result.stderr}"

                rec_dir = os.path.join(outdir, "recognition")
                assert os.path.isdir(rec_dir)
                files = sorted(os.listdir(rec_dir))
                assert len(files) == 2
                assert all(f.endswith(".m4a") for f in files)

                # Verify each file is valid audio
                for f in files:
                    fpath = os.path.join(rec_dir, f)
                    probe = subprocess.run(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-show_entries",
                            "stream=codec_name",
                            "-of",
                            "default=noprint_wrappers=1:nokey=1",
                            fpath,
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    assert probe.stdout.strip() == "aac"
        finally:
            os.remove(vocab_path)

    @skip_no_say
    @skip_no_ffmpeg
    def test_file_source_batch_mode(self):
        """Full pipeline: text file → batch m4a file."""
        text = "0001\nHello\nnǐ hǎo\n你好\n\n0002\nThank you\nxiè xiè\n谢谢\n"
        vocab_path = write_vocab_file(text)
        try:
            with tempfile.TemporaryDirectory() as outdir:
                result = subprocess.run(
                    [
                        "python3",
                        "generate-practice-audio.py",
                        "--source",
                        "file",
                        "--file",
                        vocab_path,
                        "--mode",
                        "production",
                        "--batch",
                        "10",
                        "--output",
                        outdir,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                assert result.returncode == 0, f"Script failed:\n{result.stderr}"

                prod_dir = os.path.join(outdir, "production")
                assert os.path.isdir(prod_dir)
                files = os.listdir(prod_dir)
                assert len(files) == 1
                assert files[0].endswith(".m4a")
                assert "batch" in files[0]

                # Batch of 2 items should be longer than a single item
                fpath = os.path.join(prod_dir, files[0])
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        fpath,
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                duration = float(probe.stdout.strip())
                # 2 items × (~1s speech + 3s pause + ~1s speech) + 2s separator ≈ 12s minimum
                assert duration > 8.0, f"Batch too short: {duration}s"
        finally:
            os.remove(vocab_path)
