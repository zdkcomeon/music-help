import unittest

from music_help import subtitles


class SubtitleLyricsTest(unittest.TestCase):
    def test_srt_tags_music_notes_and_credit_lines(self):
        text = """1
00:00:01,875 --> 00:00:04,516
<i><font color="#80ffff">Vod2smi </Font>by <font color="Lime">Blue-Bird♪</font>

2
00:00:16,000 --> 00:00:20,958
<i>♪ It's been a long time ♪</i>

3
00:00:16,000 --> 00:00:18,000
It's you and me

4
00:00:20,000 --> 00:00:21,000
[Taylor Swift "Song" playing]
"""
        self.assertEqual(subtitles.lyrics_from_subtitle(text), "[00:16.00] It's been a long time It's you and me")

    def test_webvtt_multiline_cues(self):
        text = """WEBVTT

cue-1
00:16.000 --> 00:20.958 align:start position:0%
It's me, hi
I'm the problem, it's me
"""
        self.assertEqual(subtitles.lyrics_from_subtitle(text), "[00:16.00] It's me, hi I'm the problem, it's me")

    def test_ass_dialogue_and_override_tags(self):
        text = """[Script Info]
Title: Sample

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:16.00,0:00:20.95,Default,,0,0,0,,{\\i1}It's me, hi\\N I'm the problem{\\i0}
Comment: 0,0:00:30.00,0:00:31.00,Default,,0,0,0,,ignored
"""
        self.assertEqual(subtitles.lyrics_from_subtitle(text), "[00:16.00] It's me, hi I'm the problem")

    def test_lrc_minutes_can_exceed_an_hour(self):
        text = '1\n03:30:24,516 --> 03:30:26,000\n"Happy birthday"\n'
        self.assertEqual(subtitles.lyrics_from_subtitle(text), '[210:24.52] "Happy birthday"')

    def test_repeated_lines_are_kept_only_when_far_apart(self):
        text = """1
00:00:10,000 --> 00:00:12,000
Same line

2
00:00:12,500 --> 00:00:14,000
Same line

3
00:01:10,000 --> 00:01:12,000
Same line
"""
        self.assertEqual(subtitles.lyrics_from_subtitle(text), "[00:10.00] Same line\n[01:10.00] Same line")

    def test_styled_lyrics_are_preferred_when_marking_is_consistent(self):
        text = """1
00:00:10,000 --> 00:00:11,000
<font color=Lime>♪ First line ♪</font>

2
00:00:12,000 --> 00:00:13,000
<font color=Lime>♪ Second line ♪</font>

3
00:00:14,000 --> 00:00:15,000
<font color=Lime>♪ Third line ♪</font>

4
00:00:16,000 --> 00:00:17,000
<font color=Lime>♪ Fourth line ♪</font>

5
00:00:18,000 --> 00:00:19,000
Some stage banter
"""
        self.assertEqual(
            subtitles.lyrics_from_subtitle(text),
            "[00:10.00] First line\n[00:12.00] Second line\n[00:14.00] Third line\n[00:16.00] Fourth line",
        )

    def test_unstyled_lines_are_kept_when_marking_is_sparse(self):
        text = """1
00:00:10,000 --> 00:00:11,000
<font color=Lime>♪ Styled line ♪</font>

2
00:00:12,000 --> 00:00:13,000
Plain line

3
00:00:14,000 --> 00:00:15,000
Another plain line
"""
        self.assertEqual(
            subtitles.lyrics_from_subtitle(text),
            "[00:10.00] Styled line\n[00:12.00] Plain line\n[00:14.00] Another plain line",
        )

    def test_dash_prefixed_sound_cues_are_dropped(self):
        text = """1
00:00:10,000 --> 00:00:11,000
- [timer ticking]

2
00:00:12,000 --> 00:00:13,000
♪ First line ♪
"""
        self.assertEqual(subtitles.lyrics_from_subtitle(text), "[00:12.00] First line")

    def test_empty_and_untimed_text_return_none(self):
        self.assertIsNone(subtitles.lyrics_from_subtitle(""))
        self.assertIsNone(subtitles.lyrics_from_subtitle("No timestamps here."))
        self.assertIsNone(subtitles.lyrics_from_subtitle("1\n00:00:01,000 --> 00:00:02,000\n[applause]\n"))


if __name__ == "__main__":
    unittest.main()
