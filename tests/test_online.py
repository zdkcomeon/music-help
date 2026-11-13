import io
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import app as application
from music_help import online


class OnlineSearchTest(unittest.TestCase):
    def test_netease_song_and_album_candidates(self):
        song = {"id": 9007199254740993, "name": "Song", "duration": 245000, "artists": [{"name": "Artist"}], "album": {"name": "Album", "publishTime": 1577836800000}}
        album = {"id": 123, "name": "Album", "artists": [{"name": "Artist"}], "publishTime": 1577836800000}
        with patch.object(online, "fetch", side_effect=[{"code": 200, "result": {"songs": [song]}}, {"code": 200, "result": {"albums": [album]}}]) as fetch:
            track = online.netease_search("Song", "Artist", "test")[0]
            release = online.netease_search("Album", "Artist", "test", albums=True)[0]
        self.assertEqual((track["neteaseId"], track["duration"], track["album"], track["year"]), ("9007199254740993", 245, "Album", "2020"))
        self.assertEqual((release["kind"], release["neteaseAlbumId"], release["title"]), ("release", "123", "Album"))
        self.assertEqual(parse_qs(urlsplit(fetch.call_args_list[1].args[0]).query)["type"], ["10"])

    def test_subtitle_search_filters_incomplete_and_orders_languages(self):
        payload = {"title": {"name": "Film", "year": 2023}, "subtitles": {"items": [
            {"id": 2, "language": "de", "format": "srt", "duration_s": 12624, "cues": 3000, "release_name": "german", "encoding": "utf-8"},
            {"id": 1, "language": "en", "format": "srt", "duration_s": 12597, "cues": 3000, "release_name": "english", "encoding": "utf-8-sig"},
            {"id": 3, "language": "en", "format": "srt", "duration_s": 12624, "cues": 3000, "release_name": "english full", "encoding": "utf-8"},
            {"id": 4, "language": "en", "format": "srt", "duration_s": 11638, "cues": 166, "release_name": "partial", "encoding": "utf-8"},
            {"id": 5, "language": "en", "format": "smi", "duration_s": 0, "cues": 3, "release_name": "broken"},
            {"id": 6, "language": "en", "format": "smi", "duration_s": 12624, "cues": 3000, "release_name": "sami"},
        ]}}
        with patch.object(online, "fetch", return_value=payload) as fetch:
            candidates = online.subtitle_search("Film", "", "test")
        self.assertEqual([candidate["subtitleId"] for candidate in candidates], ["3", "1", "2"])
        self.assertEqual((candidates[0]["kind"], candidates[0]["title"], candidates[0]["year"], candidates[0]["duration"]), ("subtitle", "Film", "2023", 12624))
        self.assertEqual(parse_qs(urlsplit(fetch.call_args.args[0]).query)["q"], ["Film"])

    def test_subtitle_search_without_match_returns_no_candidates(self):
        with patch.object(online, "fetch", return_value={"error": "not_found"}):
            self.assertEqual(online.subtitle_search("Unknown", "", "test"), [])
        with patch.object(online, "fetch") as fetch:
            self.assertEqual(online.subtitle_search("", "", "test"), [])
            fetch.assert_not_called()

    def test_subtitle_source_is_opt_in(self):
        with patch.object(online, "netease_search", return_value=[]), patch.object(online, "musicbrainz_search", return_value=[]), patch.object(online, "musicbrainz_release_search", return_value=[]), patch.object(online, "lrclib_search", return_value=[]), patch.object(online, "subtitle_search") as subtitle:
            online.search("Song", "Artist", "test")
        subtitle.assert_not_called()

    def test_subtitle_lyrics_are_converted_to_lrc(self):
        body = "1\n00:00:16,000 --> 00:00:20,958\n<i>♪ It's me, hi ♪</i>\n"
        with patch.object(online, "fetch", return_value=(body.encode("utf-8"), "application/x-subrip")) as fetch:
            lyrics = online.get_lyrics({"kind": "subtitle", "subtitleId": "1846419713", "encoding": "utf-8-sig"}, "test")
        self.assertEqual(lyrics, "[00:16.00] It's me, hi")
        self.assertEqual(fetch.call_args.args[0], "https://api.thesubtitledb.org/get/1846419713")
        self.assertTrue(fetch.call_args.kwargs["binary"])

    def test_subtitle_lyrics_reject_invalid_id_and_unsupported_type(self):
        with patch.object(online, "fetch") as fetch:
            for subtitle_id in ("../x", "12x", 12, ""):
                with self.subTest(subtitle_id=subtitle_id):
                    self.assertIsNone(online.get_lyrics({"kind": "subtitle", "subtitleId": subtitle_id}, "test"))
            fetch.assert_not_called()
        with patch.object(online, "fetch", return_value=(b"PK\x03\x04", "application/zip")):
            self.assertIsNone(online.get_lyrics({"kind": "subtitle", "subtitleId": "1"}, "test"))

    def test_subtitle_lyrics_decode_with_item_encoding(self):
        body = "1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("gbk")
        with patch.object(online, "fetch", return_value=(body, "application/x-subrip")):
            lyrics = online.get_lyrics({"kind": "subtitle", "subtitleId": "1", "encoding": "gbk"}, "test")
        self.assertEqual(lyrics, "[00:01.00] 你好")

    def test_release_and_long_audio_do_not_fetch_single_song_lyrics(self):
        with patch.object(online, "fetch") as fetch:
            self.assertIsNone(online.get_lyrics({"kind": "release", "neteaseAlbumId": "123"}, "test"))
            self.assertIsNone(online.get_lyrics({"kind": "recording", "neteaseId": "456", "skipLyrics": True}, "test"))
            fetch.assert_not_called()
        with patch.object(online, "fetch", return_value={"lrc": {"lyric": "sample lyrics"}}):
            self.assertEqual(online.get_lyrics({"neteaseId": "456"}, "test"), "sample lyrics")

    def test_cover_fetch_restricts_provider_host_and_redirect(self):
        candidate = {"neteaseId": "456"}
        with patch.object(online, "fetch", side_effect=[{"songs": [{"album": {"picUrl": "http://p1.music.126.net/test.jpg?secret=123"}}]}, (b"image", "image/jpg")]) as fetch:
            self.assertEqual(online.get_cover(candidate, "test")["mime"], "image/jpeg")
        self.assertEqual(fetch.call_args.args[0], "https://p1.music.126.net/test.jpg?secret=123")
        self.assertTrue(fetch.call_args.kwargs["restricted_cover"])
        for url in ("https://music.126.net.attacker.example/image", "https://user:password@p1.music.126.net/image", "http://127.0.0.1/image", "https://p1.music.126.net:444/image"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                online._safe_netease_image_url(url)
        self.assertEqual(online._safe_log_value({"token": "secret", "url": "https://example.com/image?token=secret"}), {"token": "<redacted>", "url": "https://example.com/image"})

    def test_partial_failure_keeps_other_results(self):
        with patch.object(online, "netease_search", return_value=[{"title": "Song"}]), patch.object(online, "musicbrainz_search", side_effect=OSError("offline")), patch.object(online, "musicbrainz_release_search", return_value=[]), patch.object(online, "lrclib_search", return_value=[]):
            result = online.search("Song", "Artist", "test")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["warnings"], ["MusicBrainz 歌曲 暂时不可用"])

    def test_musicbrainz_retry_redacts_credentials(self):
        url = "https://musicbrainz.org/ws/2/recording/?query=Song"
        error = HTTPError(url, 503, "Unavailable", {}, io.BytesIO(b'{"message":"temporary", "token":"private-value"}'))
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"recordings": []}'
        response.headers.get_content_type.return_value = "application/json"
        response.status = 200
        with patch.object(online, "_open_request", side_effect=[error, response]), patch.object(online.time, "sleep"), self.assertLogs(online.logger, level="INFO") as logs:
            self.assertEqual(online.fetch(url, "test"), {"recordings": []})
        self.assertIn("online.request_retry", " ".join(logs.output))
        self.assertNotIn("private-value", " ".join(logs.output))

    def test_musicbrainz_release_query_and_cover(self):
        group_id = "99b09d02-9cc9-3fed-8431-f162165a9371"
        response = {"release-groups": [{"id": group_id, "title": "Album", "first-release-date": "2021-01-01", "artist-credit": [{"name": "Artist"}]}]}
        with patch.object(online, "fetch", return_value=response) as fetch:
            candidate = online.musicbrainz_release_search("Album", "Artist", "test")[0]
        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)["query"][0]
        self.assertIn('alias:"Album"', query)
        self.assertIn('artist:"Artist"', query)
        self.assertEqual((candidate["kind"], candidate["year"]), ("release", "2021"))
        with patch.object(online, "fetch", return_value=(b"image", "image/png")) as fetch:
            online.get_cover(candidate, "test")
        self.assertIn(f"/release-group/{group_id}/front-500", fetch.call_args.args[0])

    def test_search_api_accepts_artist_only_and_validates_source(self):
        client = application.app.test_client()
        with patch.object(online, "search", return_value={"candidates": [], "warnings": []}) as search:
            response = client.get("/api/online/search", query_string={"artist": "Artist", "source": "netease_album", "requestId": "sample-123"})
            subtitle = client.get("/api/online/search", query_string={"title": "Film", "source": "subtitle"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(subtitle.status_code, 200)
        search.assert_any_call("", "Artist", "sample-123", "netease_album")
        self.assertEqual((search.call_args.args[0], search.call_args.args[1], search.call_args.args[3]), ("Film", "", "subtitle"))
        self.assertEqual(client.get("/api/online/search").status_code, 400)
        self.assertEqual(client.get("/api/online/search?title=Song&source=invalid").status_code, 400)
        headers = {"X-Music-Help-Token": application.csrf_token}
        self.assertEqual(client.post("/api/online/details", json={"neteaseId": "https://example.com"}, headers=headers).status_code, 400)
        self.assertEqual(client.post("/api/online/details", json={"subtitleId": "../1"}, headers=headers).status_code, 400)


if __name__ == "__main__":
    unittest.main()
