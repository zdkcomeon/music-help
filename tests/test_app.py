import base64
import io
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

from mutagen.id3 import APIC, ID3, TIT2, TPE1, TXXX, USLT

import app as application
from music_help import online
from music_help.tags import read_track, update_tags


# Valid MPEG frame headers let Mutagen read the test file without an encoder.
MP3_BYTES = (bytes.fromhex("fffb9000") + bytes(413)) * 20


class MusicHelpTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        application.entries.clear()
        application.library_root = self.root
        self.client = application.app.test_client()
        self.headers = {"X-Music-Help-Token": application.csrf_token}

    def tearDown(self):
        application.entries.clear()
        application.library_root = None
        self.directory.cleanup()

    def make_track(self, name):
        path = self.root / name
        path.write_bytes(MP3_BYTES)
        tags = ID3()
        tags.add(TIT2(encoding=1, text=["旧歌名"]))
        tags.add(TPE1(encoding=1, text=["原艺术家"]))
        tags.add(TXXX(encoding=1, desc="custom", text=["保留"]))
        tags.add(USLT(encoding=1, lang="zho", desc="zh", text="旧歌词"))
        tags.add(APIC(encoding=1, mime="image/jpeg", type=4, desc="back", data=b"back"))
        tags.save(path, v2_version=3)
        return path

    def test_save_preserves_other_frames_and_rejects_stale_file(self):
        path = self.make_track("song.mp3")
        before = read_track(path, path.name, "library")
        cover = {"data": base64.b64encode(b"\xff\xd8\xfffront").decode(), "mime": "image/jpeg"}
        update_tags(path, before["version"], {"title": "新歌名", "lyrics": "新歌词"}, True, cover, "test")
        after = read_track(path, path.name, "library")
        tags = ID3(path, v2_version=3)
        self.assertEqual((after["title"], after["artist"], after["lyrics"]), ("新歌名", "原艺术家", "新歌词"))
        self.assertEqual(tags.version[:2], (2, 3))
        self.assertEqual(str(tags["TXXX:custom"]), "保留")
        self.assertEqual({cover.type for cover in tags.getall("APIC")}, {3, 4})
        saved = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "其他程序修改"):
            update_tags(path, before["version"], {"title": "过期修改"}, False, None, "test")
        self.assertEqual(path.read_bytes(), saved)
        with self.assertRaisesRegex(ValueError, "年份"):
            update_tags(path, after["version"], {"year": "错误年份"}, False, None, "test")
        self.assertEqual(path.read_bytes(), saved)

    def test_batch_save_keeps_unmodified_fields_and_reports_conflicts(self):
        first = self.make_track("first.mp3")
        second = self.make_track("second.mp3")
        tracks = self.client.get("/api/library").get_json()["tracks"]
        selected = [{"id": track["id"], "version": track["version"]} for track in tracks]
        second.write_bytes(second.read_bytes() + b"changed")
        response = self.client.post("/api/tags", json={"files": selected, "changes": {"artist": "统一艺术家"}}, headers=self.headers)
        result = response.get_json()
        self.assertEqual((len(result["tracks"]), len(result["errors"])), (1, 1))
        self.assertEqual(read_track(first, first.name, "library")["artist"], "统一艺术家")
        self.assertEqual(read_track(second, second.name, "library")["artist"], "原艺术家")
        self.assertEqual(read_track(first, first.name, "library")["title"], "旧歌名")

    def test_invalid_audio_and_rename_never_overwrites(self):
        response = self.client.post("/api/files", data={"file": (io.BytesIO(b"\x00\x00\x00\x18ftyp" + bytes(100)), "wrong.mp3")}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("无法识别音频格式", response.get_json()["error"])
        self.make_track("first.mp3")
        target = self.make_track("原艺术家 - 旧歌名.mp3")
        target_bytes = target.read_bytes()
        tracks = self.client.get("/api/library").get_json()["tracks"]
        track = next(item for item in tracks if item["name"] == "first.mp3")
        response = self.client.post("/api/rename", json={"files": [{"id": track["id"], "version": track["version"]}], "template": "%artist% - %title%"}, headers=self.headers)
        self.assertEqual(response.get_json()["errors"][0]["error"], "目标文件已存在。")
        self.assertTrue((self.root / "first.mp3").exists())
        self.assertEqual(target.read_bytes(), target_bytes)
        with patch.object(application.os, "link", side_effect=OSError("hard links unsupported")):
            response = self.client.post("/api/rename", json={"files": [{"id": track["id"], "version": track["version"]}], "template": "%filename% - copy"}, headers=self.headers)
        self.assertFalse(response.get_json()["errors"])
        self.assertTrue((self.root / "first - copy.mp3").exists())
        self.assertFalse((self.root / "first.mp3").exists())

    def test_uploaded_files_can_be_edited_and_downloaded_as_zip(self):
        tracks = []
        for _ in range(2):
            response = self.client.post("/api/files", data={"file": (io.BytesIO(MP3_BYTES), "song.mp3")}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            tracks.append(response.get_json()["track"])
        payload = {"files": [{"id": track["id"], "version": track["version"]} for track in tracks], "changes": {"title": "批量歌曲"}}
        result = self.client.post("/api/tags", json=payload, headers=self.headers).get_json()
        self.assertFalse(result["errors"])
        response = self.client.get("/api/download", query_string=[("id", track["id"]) for track in tracks])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ["批量歌曲.mp3", "批量歌曲 (2).mp3"])
            self.assertEqual(str(ID3(io.BytesIO(archive.read("批量歌曲.mp3")))["TIT2"]), "批量歌曲")
        response.close()

    def test_saved_tags_update_upload_download_name_and_extension(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            output.writeframes(bytes(8820))
        buffer.seek(0)
        uploaded = self.client.post("/api/files", data={"file": (buffer, "fake.mp3")}, headers=self.headers).get_json()["track"]
        self.assertTrue(uploaded["extensionMismatch"])
        result = self.client.post(
            "/api/tags",
            json={"files": [{"id": uploaded["id"], "version": uploaded["version"]}], "changes": {"title": "新歌名", "artist": "艺术家", "lyrics": "新歌词"}},
            headers=self.headers,
        ).get_json()
        self.assertFalse(result["errors"])
        self.assertEqual(result["tracks"][0]["name"], "艺术家 - 新歌名.wav")
        response = self.client.get("/api/download", query_string=[("id", uploaded["id"])])
        self.assertIn("艺术家 - 新歌名.wav", unquote(response.headers["Content-Disposition"]))
        path = self.root / "downloaded.wav"
        path.write_bytes(response.data)
        response.close()
        downloaded = read_track(path, path.name, "upload")
        self.assertEqual((downloaded["title"], downloaded["artist"], downloaded["lyrics"]), ("新歌名", "艺术家", "新歌词"))

    def test_explicit_rename_is_not_overwritten_by_later_save(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            output.writeframes(bytes(8820))
        buffer.seek(0)
        uploaded = self.client.post("/api/files", data={"file": (buffer, "fake.mp3")}, headers=self.headers).get_json()["track"]
        renamed = self.client.post(
            "/api/rename",
            json={"files": [{"id": uploaded["id"], "version": uploaded["version"]}], "template": "%filename% - 自定义", "useDetectedExtension": True},
            headers=self.headers,
        ).get_json()
        self.assertEqual(renamed["tracks"][0]["name"], "fake - 自定义.wav")
        result = self.client.post(
            "/api/tags",
            json={"files": [{"id": uploaded["id"], "version": renamed["tracks"][0]["version"]}], "changes": {"title": "新歌名"}},
            headers=self.headers,
        ).get_json()
        self.assertEqual(result["tracks"][0]["name"], "fake - 自定义.wav")

    def test_online_response_with_missing_optional_fields(self):
        with patch.object(online, "fetch", return_value={"recordings": [{"title": "Song", "length": None, "releases": [{"date": None}]}]}):
            candidate = online.musicbrainz_search("Song", "", "test")[0]
        self.assertEqual((candidate["duration"], candidate["year"], candidate["album"]), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
