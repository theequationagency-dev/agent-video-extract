"""Caption parsing: rolling auto-caption collapse, tags, entities, SRT."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import video_metadata_extractor as vme  # noqa: E402


# A faithful sample of YouTube's rolling automatic captions: each cue repeats
# the previous cue's text and appends a few more words, with inline karaoke
# timing tags. Naive parsing triples every phrase.
ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.030 --> 00:00:02.669 align:start position:0%
 
the<00:00:00.719><c> quick</c><00:00:01.199><c> brown</c><00:00:01.859><c> fox</c>

00:00:02.669 --> 00:00:02.679 align:start position:0%
the quick brown fox
 

00:00:02.679 --> 00:00:05.009 align:start position:0%
the quick brown fox
jumps<00:00:03.240><c> over</c><00:00:03.780><c> the</c><00:00:04.319><c> lazy</c><00:00:04.800><c> dog</c>

00:00:05.009 --> 00:00:05.019 align:start position:0%
jumps over the lazy dog
 

00:00:05.019 --> 00:00:07.500 align:start position:0%
jumps over the lazy dog
and<00:00:05.500><c> keeps</c><00:00:06.000><c> running</c>
"""

GROWING_VTT = """WEBVTT

00:00:00.000 --> 00:00:01.000
hello

00:00:01.000 --> 00:00:02.000
hello world

00:00:02.000 --> 00:00:03.000
hello world again
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
First line

2
00:00:03,500 --> 00:00:05,250
Second line
"""


class TimestampTests(unittest.TestCase):
    def test_hms(self):
        self.assertAlmostEqual(vme.parse_timestamp("00:01:02.500"), 62.5)

    def test_ms_only(self):
        self.assertAlmostEqual(vme.parse_timestamp("01:02.250"), 62.25)

    def test_comma_separator(self):
        self.assertAlmostEqual(vme.parse_timestamp("00:00:03,750"), 3.75)

    def test_hours(self):
        self.assertAlmostEqual(vme.parse_timestamp("01:00:00.000"), 3600.0)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            vme.parse_timestamp("not:a:timestamp")


class CleanTextTests(unittest.TestCase):
    def test_strips_karaoke_tags(self):
        raw = "the<00:00:00.719><c> quick</c><00:00:01.199><c> brown</c>"
        self.assertEqual(vme.clean_caption_text(raw), "the quick brown")

    def test_strips_styling_and_voice_tags(self):
        raw = '<v Narrator><c.colorE5E5E5>steady on</c></v>'
        self.assertEqual(vme.clean_caption_text(raw), "steady on")

    def test_unescapes_entities(self):
        self.assertEqual(vme.clean_caption_text("rock &amp; roll &#39;n&#39; blues"),
                         "rock & roll 'n' blues")

    def test_collapses_whitespace(self):
        self.assertEqual(vme.clean_caption_text("  too    many\tspaces "), "too many spaces")

    def test_keeps_plain_comparisons(self):
        self.assertEqual(vme.clean_caption_text("5 > 3 and 2 &lt; 4"), "5 > 3 and 2 < 4")


class ParseVttTests(unittest.TestCase):
    def test_parses_cue_count_and_bounds(self):
        cues = vme.parse_vtt(ROLLING_VTT)
        self.assertEqual(len(cues), 5)
        self.assertAlmostEqual(cues[0]["start"], 0.03)
        self.assertAlmostEqual(cues[-1]["end"], 7.5)

    def test_skips_header_block(self):
        cues = vme.parse_vtt(ROLLING_VTT)
        self.assertNotIn("Kind: captions", [line for cue in cues for line in cue["lines"]])

    def test_skips_note_blocks(self):
        data = "WEBVTT\n\nNOTE this is a comment\nwith --> inside it\n\n" \
               "00:00:01.000 --> 00:00:02.000\nreal text\n"
        cues = vme.parse_vtt(data)
        self.assertEqual([cue["lines"] for cue in cues], [["real text"]])

    def test_handles_bytes_and_bom(self):
        cues = vme.parse_vtt(("﻿" + GROWING_VTT).encode("utf-8"))
        self.assertEqual(len(cues), 3)

    def test_empty_input(self):
        self.assertEqual(vme.parse_vtt(""), [])
        self.assertEqual(vme.parse_vtt(None), [])

    def test_parses_srt(self):
        cues = vme.parse_vtt(SRT)
        self.assertEqual([cue["lines"] for cue in cues], [["First line"], ["Second line"]])
        self.assertAlmostEqual(cues[1]["start"], 3.5)


class MergeTests(unittest.TestCase):
    def test_rolling_captions_are_merged_not_tripled(self):
        segments, transcript = vme.parse_captions(ROLLING_VTT)
        self.assertEqual([segment["text"] for segment in segments], [
            "the quick brown fox",
            "jumps over the lazy dog",
            "and keeps running",
        ])
        self.assertEqual(transcript,
                         "the quick brown fox jumps over the lazy dog and keeps running")
        self.assertEqual(transcript.count("the quick brown fox"), 1)

    def test_growing_single_line_captions(self):
        segments, transcript = vme.parse_captions(GROWING_VTT)
        self.assertEqual([segment["text"] for segment in segments],
                         ["hello", "world", "again"])
        self.assertEqual(transcript, "hello world again")

    def test_segments_carry_original_timings(self):
        segments, _ = vme.parse_captions(ROLLING_VTT)
        self.assertEqual(segments[0]["start"], 0.03)
        self.assertEqual(segments[1]["start"], 2.679)
        self.assertEqual(segments[2]["end"], 7.5)
        for segment in segments:
            self.assertLessEqual(segment["start"], segment["end"])
            self.assertEqual(set(segment), {"start", "end", "text"})

    def test_repeated_speech_survives(self):
        # Genuine repetition is not caption rolling and must not be eaten.
        data = ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nvery very\n\n"
                "00:00:01.000 --> 00:00:02.000\nvery good\n")
        segments, transcript = vme.parse_captions(data)
        self.assertEqual(transcript, "very very very good")
        self.assertEqual(len(segments), 2)

    def test_consecutive_identical_cues_are_one_segment(self):
        data = ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n[music]\n\n"
                "00:00:02.000 --> 00:00:04.000\n[music]\n")
        segments, transcript = vme.parse_captions(data)
        self.assertEqual(transcript, "[music]")
        self.assertEqual(len(segments), 1)

    def test_strip_repeated_prefix_is_conservative(self):
        self.assertEqual(vme.strip_repeated_prefix("a b", "a b c"), "c")
        self.assertEqual(vme.strip_repeated_prefix("a b", "a b"), "")
        self.assertEqual(vme.strip_repeated_prefix("a b", "b c"), "b c")
        self.assertEqual(vme.strip_repeated_prefix("", "a b"), "a b")


class LanguageTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(vme.normalize_language("en-US"), "en")
        self.assertEqual(vme.normalize_language("en_GB"), "en")
        self.assertEqual(vme.normalize_language("en-orig"), "en")
        self.assertEqual(vme.normalize_language(None), "")

    def test_loose_match(self):
        self.assertTrue(vme.languages_match("en", "en-US"))
        self.assertTrue(vme.languages_match("en-US", "en"))
        self.assertTrue(vme.languages_match("EN", "en-GB"))
        self.assertFalse(vme.languages_match("en", "es-419"))

    def test_prefers_manual_over_automatic(self):
        info = {
            "subtitles": {"en-GB": [{"ext": "vtt", "url": "manual.vtt"}]},
            "automatic_captions": {"en": [{"ext": "vtt", "url": "auto.vtt"}]},
        }
        track = vme.select_subtitle_track(info, ["en"])
        self.assertEqual(track["source"], "manual")
        self.assertEqual(track["url"], "manual.vtt")

    def test_exact_tag_beats_loose_tag(self):
        info = {"subtitles": {
            "en-US": [{"ext": "vtt", "url": "us.vtt"}],
            "en": [{"ext": "vtt", "url": "plain.vtt"}],
        }}
        self.assertEqual(vme.select_subtitle_track(info, ["en-US"])["url"], "us.vtt")

    def test_automatic_used_when_no_manual(self):
        info = {"subtitles": {}, "automatic_captions": {
            "en-orig": [{"ext": "vtt", "url": "auto.vtt"}]}}
        track = vme.select_subtitle_track(info, ["en"])
        self.assertEqual((track["source"], track["language"]), ("automatic", "en-orig"))

    def test_auto_can_be_disabled(self):
        info = {"subtitles": {}, "automatic_captions": {
            "en": [{"ext": "vtt", "url": "auto.vtt"}]}}
        self.assertIsNone(vme.select_subtitle_track(info, ["en"], allow_auto=False))

    def test_any_language_fallback(self):
        info = {"subtitles": {"ja": [{"ext": "vtt", "url": "ja.vtt"}]}}
        self.assertIsNone(vme.select_subtitle_track(info, ["en"]))
        self.assertEqual(vme.select_subtitle_track(info, ["en"], allow_any=True)["language"], "ja")

    def test_unparseable_formats_are_ignored(self):
        info = {"subtitles": {"en": [{"ext": "json3", "url": "x.json3"}]}}
        self.assertIsNone(vme.select_subtitle_track(info, ["en"]))

    def test_vtt_preferred_over_srt(self):
        info = {"subtitles": {"en": [
            {"ext": "srt", "url": "x.srt"}, {"ext": "vtt", "url": "x.vtt"}]}}
        self.assertEqual(vme.select_subtitle_track(info, ["en"])["ext"], "vtt")


if __name__ == "__main__":
    unittest.main()
