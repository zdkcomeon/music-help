import base64
import hashlib
import io
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from mutagen.aac import AAC
from mutagen.apev2 import APEBinaryValue, APEv2File
from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TIT2, TXXX
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

import app as application
from music_help.tags import ID3_FORMATS, audio_cover, load_audio, read_track, update_tags

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = shutil.which("ffmpeg")


class FormatTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        application.entries.clear()
        application.library_root = self.root
        self.client = application.app.test_client()
        self.headers = {"X-Music-Help-Token": application.csrf_token}
        self.wave_file = self.root / "source.wav"
        with wave.open(str(self.wave_file), "wb") as output:
            output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            output.writeframes(bytes(8820))

    def tearDown(self):
        application.entries.clear()
        application.library_root = None
        self.directory.cleanup()

    def test_wave_detected_without_extension(self):
        path = self.root / "no-extension"
        shutil.copyfile(self.wave_file, path)
        before = read_track(path, path.name, "library")
        self.assertEqual(before["format"], "WAV")
        self.assertTrue(before["extensionMismatch"])
        update_tags(path, before["version"], {"title": "歌曲", "lyrics": "歌词"}, False, None, "test")
        after = read_track(path, path.name, "library")
        self.assertEqual((after["title"], after["lyrics"]), ("歌曲", "歌词"))

    @unittest.skipUnless(FFMPEG, "format regression tests require ffmpeg")
    def test_common_formats_preserve_audio_and_other_metadata(self):
        formats = [
            ("mp3", "libmp3lame"), ("aac", "aac"), ("m4a", "aac"), ("flac", "flac"), ("ogg", "libvorbis"),
            ("opus", "libopus"), ("wav", "pcm_s16le"), ("aiff", "pcm_s16be"),
            ("wma", "wmav2"), ("wv", "wavpack"), ("tta", "tta"),
        ]
        values = {"title": "歌曲", "artist": "歌手", "album": "专辑", "albumArtist": "专辑歌手", "year": "2024", "track": "3/12", "disc": "1/2", "genre": "流行", "composer": "作曲", "comment": "备注", "lyrics": "歌词"}
        cover = {"data": base64.b64encode(b"\xff\xd8\xfftest-cover").decode()}
        for suffix, codec in formats:
            with self.subTest(format=suffix):
                source = self.root / f"source.{suffix}"
                if source != self.wave_file:
                    result = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.wave_file), "-codec:a", codec, str(source)], capture_output=True)
                    if result.returncode:
                        self.skipTest(f"ffmpeg encoder unavailable: {codec}")
                path = self.root / f"wrong-{suffix}.bin"
                shutil.copyfile(source, path)
                audio = load_audio(path)
                if audio.tags is None:
                    if isinstance(audio, AAC):
                        audio.tags = ID3()
                    else:
                        audio.add_tags()
                if isinstance(audio, ID3_FORMATS):
                    audio.tags.add(TXXX(encoding=3, desc="keep", text=["preserved"]))
                elif isinstance(audio, MP4):
                    audio.tags["----:com.apple.iTunes:keep"] = [MP4FreeForm(b"preserved")]
                elif isinstance(audio, APEv2File):
                    audio.tags["keep"] = "preserved"
                else:
                    audio.tags["keep"] = ["preserved"]
                if isinstance(audio, AAC):
                    audio.tags.save(path)
                else:
                    audio.save(path)
                before = read_track(path, path.name, "library")
                original_audio = self.audio_hash(path)
                update_tags(path, before["version"], values, True, cover, "format-test")
                after = read_track(path, path.name, "library")
                for field, value in values.items():
                    self.assertEqual(after[field], value, field)
                audio = load_audio(path)
                if isinstance(audio, ID3_FORMATS):
                    self.assertEqual(str(audio.tags["TXXX:keep"]), "preserved")
                elif isinstance(audio, MP4):
                    self.assertEqual(bytes(audio.tags["----:com.apple.iTunes:keep"][0]), b"preserved")
                else:
                    self.assertIn("preserved", str(audio.tags["keep"]))
                self.assertEqual(audio_cover(audio)[1], b"\xff\xd8\xfftest-cover")
                update_tags(path, after["version"], {"title": "新标题"}, False, None, "format-test")
                after = read_track(path, path.name, "library")
                self.assertTrue(after["hasCover"])
                self.assertEqual(after["lyrics"], "歌词")
                update_tags(path, after["version"], {}, True, None, "format-test")
                self.assertIsNone(audio_cover(load_audio(path)))
                self.assertEqual(self.audio_hash(path), original_audio)

    def audio_hash(self, path):
        data = subprocess.check_output([FFMPEG, "-hide_banner", "-loglevel", "quiet", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-"])
        return hashlib.sha256(data).hexdigest()

    @unittest.skipUnless(FFMPEG, "format regression tests require ffmpeg")
    def test_wave_and_aiff_native_metadata_stays_consistent(self):
        for suffix, codec in (("wav", "pcm_s16le"), ("aiff", "pcm_s16be")):
            with self.subTest(format=suffix):
                path = self.root / f"native.{suffix}"
                subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.wave_file), "-codec:a", codec, "-metadata", "title=Old title", "-metadata", "artist=Old artist", str(path)], check=True)
                before = read_track(path, path.name, "library")
                self.assertEqual(before["title"], "Old title")
                original_audio = self.audio_hash(path)
                update_tags(path, before["version"], {"title": "新标题", "artist": "新歌手"}, False, None, "native-test")
                after = read_track(path, path.name, "library")
                self.assertEqual((after["title"], after["artist"]), ("新标题", "新歌手"))
                metadata = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
                self.assertIn("新标题", metadata)
                self.assertEqual(self.audio_hash(path), original_audio)
                update_tags(path, after["version"], {"title": ""}, False, None, "native-test")
                self.assertEqual(read_track(path, path.name, "library")["title"], "")

    @unittest.skipUnless(FFMPEG, "format regression tests require ffmpeg")
    def test_replacing_front_cover_preserves_other_pictures(self):
        formats = [("m4a", "aac"), ("flac", "flac"), ("ogg", "libvorbis"), ("wma", "wmav2"), ("wv", "wavpack")]
        back = b"\x89PNG\r\n\x1a\nback-cover"
        front = b"\xff\xd8\xffnew-front"
        for suffix, codec in formats:
            with self.subTest(format=suffix):
                path = self.root / f"cover.{suffix}"
                result = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.wave_file), "-codec:a", codec, str(path)], capture_output=True)
                if result.returncode:
                    self.skipTest(f"ffmpeg encoder unavailable: {codec}")
                audio = load_audio(path)
                if audio.tags is None:
                    audio.add_tags()
                if isinstance(audio, MP4):
                    audio.tags["covr"] = [MP4Cover(b"\xff\xd8\xffold", imageformat=MP4Cover.FORMAT_JPEG), MP4Cover(back, imageformat=MP4Cover.FORMAT_PNG)]
                elif isinstance(audio, FLAC):
                    picture = Picture()
                    picture.type, picture.mime, picture.data = 4, "image/png", back
                    audio.add_picture(picture)
                elif isinstance(audio, ASF):
                    binary = struct.pack("<BI", 4, len(back)) + "image/png".encode("utf-16-le") + b"\x00\x00\x00\x00" + back
                    audio.tags["WM/Picture"] = [ASFByteArrayAttribute(binary)]
                elif isinstance(audio, APEv2File):
                    audio.tags["Cover Art (Back)"] = APEBinaryValue(b"back.png\x00" + back)
                else:
                    picture = Picture()
                    picture.type, picture.mime, picture.data = 4, "image/png", back
                    audio.tags["metadata_block_picture"] = [base64.b64encode(picture.write()).decode()]
                audio.save(path)
                before = read_track(path, path.name, "library")
                update_tags(path, before["version"], {}, True, {"data": base64.b64encode(front).decode()}, "cover-test")
                after = read_track(path, path.name, "library")
                self.assertEqual(audio_cover(load_audio(path))[1], front)
                update_tags(path, after["version"], {}, True, None, "cover-test")
                self.assertEqual(audio_cover(load_audio(path))[1], back)

    @unittest.skipUnless(FFMPEG, "format regression tests require ffmpeg")
    def test_mp4_with_mp3_suffix_upload_rename_and_scan(self):
        source = self.root / "source.m4a"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.wave_file), "-codec:a", "aac", str(source)], check=True)
        content = source.read_bytes()
        response = self.client.post("/api/files", data={"file": (io.BytesIO(content), "sample.mp3")}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        track = response.get_json()["track"]
        self.assertEqual((track["format"], track["detectedExtension"], track["extensionMismatch"]), ("MP4 音频", ".m4a", True))
        self.assertEqual(self.client.get(f'/api/files/{track["id"]}/audio').mimetype, "audio/mp4")
        payload = {"files": [{"id": track["id"], "version": track["version"]}], "changes": {"title": "匹配歌曲", "lyrics": "歌词"}}
        result = self.client.post("/api/tags", json=payload, headers=self.headers).get_json()
        self.assertFalse(result["errors"])
        track = result["tracks"][0]
        payload = {"files": [{"id": track["id"], "version": track["version"]}], "template": "%title%", "useDetectedExtension": True}
        result = self.client.post("/api/rename", json=payload, headers=self.headers).get_json()
        self.assertEqual(result["tracks"][0]["name"], "匹配歌曲.m4a")
        response = self.client.get("/api/download", query_string={"id": track["id"]})
        downloaded = MP4(io.BytesIO(response.data))
        self.assertEqual(downloaded["\xa9nam"], ["匹配歌曲"])
        self.assertEqual(downloaded["\xa9lyr"], ["歌词"])
        response.close()
        (self.root / "without-extension").write_bytes(content)
        (self.root / "notes.txt").write_text("not audio")
        result = self.client.get("/api/library").get_json()
        self.assertTrue(any(item["name"] == "without-extension" and item["format"] == "MP4 音频" for item in result["tracks"]))
        self.assertFalse(any(item["name"] == "notes.txt" for item in result["errors"]))

    @unittest.skipUnless(FFMPEG, "format regression tests require ffmpeg")
    def test_flac_with_id3_prefix_preserves_and_updates_both_tag_types(self):
        path = self.root / "prefixed.flac"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.wave_file), "-codec:a", "flac", str(path)], check=True)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=["原始标题"]))
        tags.add(TXXX(encoding=3, desc="keep", text=["preserved"]))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover", data=b"\xff\xd8\xffold"))
        tags.save(path)
        before = read_track(path, path.name, "library")
        self.assertEqual((before["format"], before["title"], before["hasCover"]), ("FLAC", "原始标题", True))
        update_tags(path, before["version"], {"title": "新标题"}, False, None, "format-test")
        after = read_track(path, path.name, "library")
        self.assertEqual(after["title"], "新标题")
        self.assertEqual(str(ID3(path)["TIT2"]), "新标题")
        self.assertEqual(str(ID3(path)["TXXX:keep"]), "preserved")
        self.assertEqual(audio_cover(load_audio(path))[1], b"\xff\xd8\xffold")


if __name__ == "__main__":
    unittest.main()
