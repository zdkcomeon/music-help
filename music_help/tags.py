"""Identify audio containers and safely read or update their metadata."""

import base64
import binascii
import logging
import os
import re
import shutil
import struct
import tempfile
import time
from pathlib import Path

from mutagen import File, MutagenError
from mutagen._riff import RiffFile
from mutagen.aac import AAC
from mutagen.aiff import AIFF, AIFFFile
from mutagen.apev2 import APEBinaryValue, APEv2File
from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, ID3NoHeaderError, TALB, TCOM, TCON, TDRC, TPE1, TPE2, TIT2, TPOS, TRCK, USLT
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4Info
from mutagen.mp4._atom import Atoms
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggvorbis import OggVorbis
from mutagen.optimfrog import OptimFROG
from mutagen.tak import TAK
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

logger = logging.getLogger(__name__)

TEXT_FRAMES = {
    "title": TIT2, "artist": TPE1, "album": TALB, "albumArtist": TPE2,
    "year": TDRC, "track": TRCK, "disc": TPOS, "genre": TCON, "composer": TCOM,
}
COMMENT_FIELDS = {
    "title": "title", "artist": "artist", "album": "album", "albumArtist": "albumartist",
    "year": "date", "track": "tracknumber", "disc": "discnumber", "genre": "genre",
    "composer": "composer", "comment": "comment", "lyrics": "lyrics",
}
MP4_FIELDS = {
    "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumArtist": "aART",
    "year": "\xa9day", "track": "trkn", "disc": "disk", "genre": "\xa9gen",
    "composer": "\xa9wrt", "comment": "\xa9cmt", "lyrics": "\xa9lyr",
}
APE_FIELDS = {
    "title": "Title", "artist": "Artist", "album": "Album", "albumArtist": "Album Artist",
    "year": "Year", "track": "Track", "disc": "Disc", "genre": "Genre",
    "composer": "Composer", "comment": "Comment", "lyrics": "Lyrics",
}
ASF_FIELDS = {
    "title": "Title", "artist": "Author", "album": "WM/AlbumTitle", "albumArtist": "WM/AlbumArtist",
    "year": "WM/Year", "track": "WM/TrackNumber", "disc": "WM/PartOfSet", "genre": "WM/Genre",
    "composer": "WM/Composer", "comment": "Description", "lyrics": "WM/Lyrics",
}
WAVE_FIELDS = {
    "INAM": "title", "IART": "artist", "IPRD": "album", "ICRD": "year",
    "ITRK": "track", "IGNR": "genre", "IWRI": "composer", "ICMT": "comment",
}
AIFF_FIELDS = {"NAME": "title", "AUTH": "artist", "ANNO": "comment"}
FIELDS = set(COMMENT_FIELDS)
MAX_COVER_BYTES = 10 * 1024 * 1024

# The suffix is a suggested extension. Existing names remain untouched until a rename.
FORMATS = {
    MP3: ("MP3", ".mp3", {".mp3"}, "audio/mpeg"),
    AAC: ("AAC", ".aac", {".aac", ".adts", ".adif"}, "audio/aac"),
    MP4: ("MP4 音频", ".m4a", {".m4a", ".m4b", ".mp4", ".m4p"}, "audio/mp4"),
    FLAC: ("FLAC", ".flac", {".flac"}, "audio/flac"),
    OggVorbis: ("Ogg Vorbis", ".ogg", {".ogg", ".oga"}, "audio/ogg"),
    OggOpus: ("Opus", ".opus", {".opus", ".ogg"}, "audio/ogg"),
    OggFLAC: ("Ogg FLAC", ".oga", {".oga", ".ogg"}, "audio/ogg"),
    OggSpeex: ("Ogg Speex", ".spx", {".spx", ".ogg"}, "audio/ogg"),
    WAVE: ("WAV", ".wav", {".wav", ".wave"}, "audio/wav"),
    AIFF: ("AIFF", ".aiff", {".aiff", ".aif", ".aifc"}, "audio/aiff"),
    ASF: ("WMA", ".wma", {".wma", ".asf"}, "audio/x-ms-wma"),
    WavPack: ("WavPack", ".wv", {".wv"}, "audio/x-wavpack"),
    MonkeysAudio: ("APE", ".ape", {".ape"}, "audio/ape"),
    Musepack: ("Musepack", ".mpc", {".mpc", ".mp+", ".mpp"}, "audio/x-musepack"),
    OptimFROG: ("OptimFROG", ".ofr", {".ofr", ".ofs"}, "audio/x-optimfrog"),
    TAK: ("TAK", ".tak", {".tak"}, "audio/x-tak"),
    TrueAudio: ("True Audio", ".tta", {".tta"}, "audio/x-tta"),
}
AUDIO_EXTENSIONS = frozenset(suffix for _, _, extensions, _ in FORMATS.values() for suffix in extensions) | {".ac3"}
ID3_FORMATS = (MP3, AAC, WAVE, AIFF, TrueAudio)
OGG_FORMATS = (OggVorbis, OggOpus, OggFLAC, OggSpeex)


class UnrecognizedAudio(ValueError):
    """The file is not a recognizable audio container."""


def load_audio(path):
    try:
        with open(path, "rb") as stream:
            header = stream.read(10)
            trailing_format = None
            marker = header[:4]
            if header[:3] == b"ID3" and len(header) == 10 and all(byte < 128 for byte in header[6:10]):
                size = sum(byte << (7 * (3 - index)) for index, byte in enumerate(header[6:10]))
                stream.seek(10 + size)
                marker = stream.read(4)
                trailing_format = {b"TTA1": TrueAudio, b"fLaC": FLAC}.get(marker)
            if marker == b"ADIF" or (len(marker) >= 2 and marker[0] == 0xff and marker[1] & 0xf6 == 0xf0):
                trailing_format = AAC
            stream.seek(0)
            # A neutral name prevents a misleading suffix from influencing detection.
            audio = trailing_format(stream, filename="audio") if trailing_format else File(fileobj=stream, filename="audio")
            if trailing_format is FLAC:
                stream.seek(0)
                audio.id3_prefix = ID3(stream)
            if trailing_format is AAC:
                stream.seek(0)
                try:
                    audio.tags = ID3(stream)
                except ID3NoHeaderError:
                    audio.tags = None
    except MutagenError as exc:
        raise UnrecognizedAudio("无法识别音频格式，或文件已经损坏。") from exc
    if audio is None:
        raise UnrecognizedAudio("无法识别音频格式，或文件已经损坏。")
    if type(audio) not in FORMATS:
        raise ValueError(f"已识别到 {type(audio).__name__} 格式，暂不支持写入该格式的标签。")
    return audio


def file_version(path):
    stat = path.stat()
    return f"{stat.st_dev}-{stat.st_ino}-{stat.st_size}-{stat.st_mtime_ns}"


def selected_cover(tags):
    covers = tags.getall("APIC") if tags else []
    return next((cover for cover in covers if cover.type == 3), covers[0] if covers else None)


def picture_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def ogg_pictures(tags):
    pictures = []
    for index, value in enumerate(tags.get("metadata_block_picture", [])):
        try:
            pictures.append((index, Picture(base64.b64decode(value, validate=True))))
        except (ValueError, TypeError, binascii.Error, MutagenError):
            continue
    return pictures


def asf_pictures(tags):
    pictures = []
    for index, attribute in enumerate(tags.get("WM/Picture", [])):
        try:
            raw = bytes(attribute.value)
            picture_kind, length = struct.unpack_from("<BI", raw)
            offset = 5
            values = []
            for _ in range(2):
                end = offset
                while raw[end:end + 2] != b"\x00\x00":
                    if end + 2 > len(raw):
                        raise ValueError("invalid picture")
                    end += 2
                values.append(raw[offset:end].decode("utf-16-le"))
                offset = end + 2
            data = raw[offset:offset + length]
            if len(data) != length:
                raise ValueError("invalid picture size")
            pictures.append((index, picture_kind, values[0], data))
        except (ValueError, TypeError, AttributeError, UnicodeError, struct.error):
            continue
    return pictures


def first_picture(pictures):
    return next((picture for picture in pictures if picture[1] == 3), pictures[0] if pictures else None)


def audio_cover(audio):
    tags = audio.tags
    if isinstance(audio, ID3_FORMATS):
        frame = selected_cover(tags)
        return (frame.mime, frame.data) if frame else None
    if isinstance(audio, MP4):
        covers = tags.get("covr", []) if tags else []
        if covers:
            data = bytes(covers[0])
            return (picture_type(data) or "application/octet-stream", data)
        return None
    if isinstance(audio, FLAC):
        candidates = [(picture, picture.type, picture.mime, picture.data) for picture in audio.pictures]
        if not candidates and getattr(audio, "id3_prefix", None):
            frame = selected_cover(audio.id3_prefix)
            return (frame.mime, frame.data) if frame else None
    elif isinstance(audio, OGG_FORMATS):
        candidates = [(index, picture.type, picture.mime, picture.data) for index, picture in ogg_pictures(tags)]
        if not candidates and tags.get("coverart"):
            try:
                data = base64.b64decode(tags["coverart"][0], validate=True)
                return (picture_type(data) or "application/octet-stream", data)
            except (ValueError, TypeError, binascii.Error):
                return None
    elif isinstance(audio, APEv2File):
        for name in ("Cover Art (Front)", "Cover Art (Back)"):
            value = tags.get(name) if tags else None
            if value is not None and isinstance(value.value, bytes):
                data = value.value.partition(b"\x00")[2]
                if data:
                    return (picture_type(data) or "application/octet-stream", data)
        return None
    elif isinstance(audio, ASF):
        candidates = asf_pictures(tags) if tags else []
    else:
        return None
    selected = first_picture(candidates)
    return (selected[2] or picture_type(selected[3]) or "application/octet-stream", selected[3]) if selected else None


def first_value(tags, key):
    value = tags.get(key) if tags else None
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if hasattr(value, "value"):
        value = value.value
        if isinstance(value, list):
            value = value[0] if value else ""
    return str(value)


def id3_values(tags):
    values = {}
    for field, frame_type in TEXT_FRAMES.items():
        frame = tags.get(frame_type.__name__) if tags else None
        values[field] = str(frame) if frame else ""
    for field, frame_id in (("comment", "COMM"), ("lyrics", "USLT")):
        frames = tags.getall(frame_id) if tags else []
        values[field] = str(frames[0]) if frames else ""
    return values


def native_chunks(stream, audio):
    if isinstance(audio, WAVE):
        root = RiffFile(stream).root
        return [(chunk, WAVE_FIELDS) for group in root.subchunks() if group.id == "LIST" and group.name == "INFO" for chunk in group.subchunks()]
    root = AIFFFile(stream).root
    return [(chunk, AIFF_FIELDS) for chunk in root.subchunks()]


def native_values(path, audio):
    values = {}
    with open(path, "rb") as stream:
        for chunk, mapping in native_chunks(stream, audio):
            field = mapping.get(chunk.id)
            if not field or field in values or chunk.data_size > 1_000_000:
                continue
            raw = chunk.read()
            try:
                value = raw.decode("utf-16").rstrip("\x00") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.rstrip(b"\x00").decode("utf-8")
            except UnicodeError:
                value = raw.rstrip(b"\x00").decode("latin-1")
            values[field] = value
    return values


def update_native_values(path, audio, changes):
    with open(path, "r+b") as stream:
        for chunk, mapping in native_chunks(stream, audio):
            field = mapping.get(chunk.id)
            if field not in changes:
                continue
            value = changes[field]
            if not value:
                chunk.delete()
            else:
                encoded = value.encode("utf-8") + (b"\x00" if isinstance(audio, WAVE) else b"")
                chunk.resize(len(encoded))
                chunk.write(encoded)


def read_values(audio, path):
    tags = audio.tags
    if isinstance(audio, ID3_FORMATS):
        values = id3_values(tags)
        if isinstance(audio, (WAVE, AIFF)):
            native = native_values(path, audio)
            values = {field: value or native.get(field, "") for field, value in values.items()}
        return values
    if isinstance(audio, MP4):
        values = {field: first_value(tags, key) for field, key in MP4_FIELDS.items() if field not in ("track", "disc")}
        for field in ("track", "disc"):
            pairs = tags.get(MP4_FIELDS[field], []) if tags else []
            number, total = pairs[0] if pairs else (0, 0)
            values[field] = f"{number}/{total}" if total else str(number) if number else ""
        return values
    mapping = ASF_FIELDS if isinstance(audio, ASF) else APE_FIELDS if isinstance(audio, APEv2File) else COMMENT_FIELDS
    values = {field: first_value(tags, key) for field, key in mapping.items()}
    if isinstance(audio, (FLAC, *OGG_FORMATS)):
        for field, total_keys in (("track", ("tracktotal", "totaltracks")), ("disc", ("disctotal", "totaldiscs"))):
            total = next((first_value(tags, key) for key in total_keys if first_value(tags, key)), "")
            if values[field] and total and "/" not in values[field]:
                values[field] += "/" + total
    if isinstance(audio, FLAC) and getattr(audio, "id3_prefix", None):
        prefix_values = id3_values(audio.id3_prefix)
        values = {field: value or prefix_values[field] for field, value in values.items()}
    return values


def read_track(path, name, source, relative_path=""):
    version = file_version(path)
    audio = load_audio(path)
    format_name, extension, extensions, mime = FORMATS[type(audio)]
    stat = path.stat()
    duration = getattr(audio.info, "length", 0) or 0
    if isinstance(audio, MP4) and not duration:
        try:
            with open(path, "rb") as stream:
                info = MP4Info()
                info._parse_mvhd(Atoms(stream)[b"moov", b"mvhd"], stream)
                duration = info.length
        except (AttributeError, KeyError, MutagenError, OSError, ValueError, struct.error) as exc:
            logger.warning("audio.duration_unavailable path=%s format=MP4 error=%s", path, exc)
    bitrate = getattr(audio.info, "bitrate", 0) or (stat.st_size * 8 / duration if duration else 0)
    cover = audio_cover(audio)
    values = read_values(audio, path)
    if file_version(path) != version:
        raise ValueError("文件在读取期间被其他程序修改，请重新读取。")
    return {
        "name": name,
        "baseName": Path(name).stem,
        "fileExtension": Path(name).suffix,
        "source": source,
        "relativePath": relative_path,
        "version": version,
        "size": stat.st_size,
        "duration": round(duration),
        "bitrate": round(bitrate / 1000),
        "format": format_name,
        "detectedExtension": extension,
        "extensionMismatch": Path(name).suffix.lower() not in extensions,
        "mimeType": mime,
        "hasCover": cover is not None,
        **values,
    }


def decode_cover(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("data"), str):
        raise ValueError("封面数据无效。")
    encoded = value["data"]
    if len(encoded) > (MAX_COVER_BYTES * 4 // 3 + 8):
        raise ValueError("封面图片不能超过 10 MB。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("封面数据无效。") from exc
    if len(data) > MAX_COVER_BYTES:
        raise ValueError("封面图片不能超过 10 MB。")
    mime = picture_type(data)
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError("封面仅支持 JPEG 或 PNG 图片。")
    return mime, data


def update_id3(tags, changes, cover_changed, cover_data, version):
    encoding = 1 if version == 3 else 3
    for field, value in changes.items():
        if field in TEXT_FRAMES:
            frame_type = TEXT_FRAMES[field]
            tags.delall(frame_type.__name__)
            if value:
                tags.add(frame_type(encoding=encoding, text=[value]))
            continue
        frame_id = "COMM" if field == "comment" else "USLT"
        frame_type = COMM if field == "comment" else USLT
        frames = tags.getall(frame_id)
        old_frame = frames[0] if frames else None
        if old_frame:
            del tags[old_frame.HashKey]
        if value:
            tags.add(frame_type(encoding=encoding, lang=old_frame.lang if old_frame else "eng", desc=old_frame.desc if old_frame else "", text=value))
    if cover_changed:
        current = selected_cover(tags)
        if current and (cover_data is None or current.type == 3):
            del tags[current.HashKey]
        if cover_data:
            description = ""
            while "APIC:" + description in tags:
                description += "."
            tags.add(APIC(encoding=encoding, mime=cover_data[0], type=3, desc=description, data=cover_data[1]))


def new_picture(cover_data):
    picture = Picture()
    picture.type = 3
    picture.mime, picture.data = cover_data
    return picture


def remove_tag(tags, key):
    if key in tags:
        del tags[key]


def update_cover(audio, cover_data):
    tags = audio.tags
    if isinstance(audio, MP4):
        covers = list(tags.get("covr", []))
        if covers:
            covers.pop(0)
        if cover_data:
            kind = MP4Cover.FORMAT_JPEG if cover_data[0] == "image/jpeg" else MP4Cover.FORMAT_PNG
            covers.insert(0, MP4Cover(cover_data[1], imageformat=kind))
        if covers:
            tags["covr"] = covers
        else:
            remove_tag(tags, "covr")
    elif isinstance(audio, FLAC):
        pictures = list(audio.pictures)
        current = next((picture for picture in pictures if picture.type == 3), pictures[0] if pictures else None)
        if current and (not cover_data or current.type == 3):
            pictures.remove(current)
        if cover_data:
            pictures.insert(0, new_picture(cover_data))
        audio.clear_pictures()
        for picture in pictures:
            audio.add_picture(picture)
    elif isinstance(audio, OGG_FORMATS):
        blocks = list(tags.get("metadata_block_picture", []))
        pictures = [(index, picture.type, picture.mime, picture.data) for index, picture in ogg_pictures(tags)]
        current = first_picture(pictures)
        if current and (not cover_data or current[1] == 3):
            blocks.pop(current[0])
        elif not current and not cover_data and tags.get("coverart"):
            legacy = list(tags["coverart"])[1:]
            if legacy:
                tags["coverart"] = legacy
            else:
                remove_tag(tags, "coverart")
                remove_tag(tags, "coverartmime")
        if cover_data:
            blocks.insert(0, base64.b64encode(new_picture(cover_data).write()).decode("ascii"))
        if blocks:
            tags["metadata_block_picture"] = blocks
        else:
            remove_tag(tags, "metadata_block_picture")
    elif isinstance(audio, APEv2File):
        front = "Cover Art (Front)"
        back = "Cover Art (Back)"
        if cover_data:
            name = b"cover.jpg" if cover_data[0] == "image/jpeg" else b"cover.png"
            tags[front] = APEBinaryValue(name + b"\x00" + cover_data[1])
        elif front in tags:
            del tags[front]
        elif back in tags:
            del tags[back]
    elif isinstance(audio, ASF):
        values = list(tags.get("WM/Picture", []))
        current = first_picture(asf_pictures(tags))
        if current and (not cover_data or current[1] == 3):
            values.pop(current[0])
        if cover_data:
            mime, data = cover_data
            binary = struct.pack("<BI", 3, len(data)) + mime.encode("utf-16-le") + b"\x00\x00\x00\x00" + data
            values.insert(0, ASFByteArrayAttribute(binary))
        if values:
            tags["WM/Picture"] = values
        else:
            remove_tag(tags, "WM/Picture")


def update_values(audio, changes):
    tags = audio.tags
    if isinstance(audio, MP4):
        for field, value in changes.items():
            key = MP4_FIELDS[field]
            if not value:
                remove_tag(tags, key)
            elif field in ("track", "disc"):
                parts = value.split("/")
                if len(parts) > 2 or any(not part.isdigit() or int(part) > 65535 for part in parts):
                    raise ValueError("曲目和光盘编号请使用数字或数字/总数格式，数值不能超过 65535。")
                tags[key] = [(int(parts[0]), int(parts[1]) if len(parts) == 2 else 0)]
            else:
                tags[key] = [value]
        return
    mapping = ASF_FIELDS if isinstance(audio, ASF) else APE_FIELDS if isinstance(audio, APEv2File) else COMMENT_FIELDS
    for field, value in changes.items():
        key = mapping[field]
        if isinstance(audio, (FLAC, *OGG_FORMATS)) and field in ("track", "disc"):
            total_key, alternate = ("tracktotal", "totaltracks") if field == "track" else ("disctotal", "totaldiscs")
            remove_tag(tags, total_key)
            remove_tag(tags, alternate)
            parts = value.split("/", 1)
            value = parts[0]
            if len(parts) == 2 and parts[1]:
                tags[total_key] = [parts[1]]
        if value:
            tags[key] = [value] if not isinstance(audio, APEv2File) else value
        else:
            remove_tag(tags, key)


def update_tags(path, expected_version, changes, cover_changed, cover, operation_id):
    path = Path(path)
    started = time.monotonic()
    logger.info("tags.save_started operation_id=%s file=%s fields=%s cover_changed=%s", operation_id, path, list(changes), cover_changed)
    if file_version(path) != expected_version:
        raise ValueError("文件已被其他程序修改，请重新加载后再保存。")
    if set(changes) - FIELDS or any(not isinstance(value, str) for value in changes.values()):
        raise ValueError("标签字段或内容无效。")
    if any(len(value) > 1_000_000 for value in changes.values()):
        raise ValueError("标签内容过长。")
    if changes.get("year") and not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", changes["year"]):
        raise ValueError("年份请使用 YYYY、YYYY-MM 或 YYYY-MM-DD 格式。")
    cover_data = decode_cover(cover) if cover_changed else None
    temp_name = None
    try:
        handle, temp_name = tempfile.mkstemp(prefix=".music-help-", dir=path.parent)
        os.close(handle)
        shutil.copy2(path, temp_name)
        logger.info("tags.copy_completed operation_id=%s file=%s bytes=%s elapsed_ms=%s", operation_id, path, path.stat().st_size, round((time.monotonic() - started) * 1000))
        audio = load_audio(temp_name)
        version = 4
        if isinstance(audio, ID3_FORMATS):
            if audio.tags and audio.tags.version[1] == 3:
                version = 3
                if isinstance(audio, AAC):
                    audio.tags = ID3(temp_name, v2_version=3)
                else:
                    audio = type(audio)(temp_name, v2_version=3)
            if audio.tags is None:
                if isinstance(audio, AAC):
                    audio.tags = ID3()
                else:
                    audio.add_tags()
            update_id3(audio.tags, changes, cover_changed, cover_data, version)
            if isinstance(audio, AAC):
                audio.tags.save(temp_name, v2_version=version)
            else:
                audio.save(temp_name, v2_version=version)
            if isinstance(audio, (WAVE, AIFF)):
                update_native_values(temp_name, audio, changes)
        else:
            if isinstance(audio, FLAC) and getattr(audio, "id3_prefix", None):
                version = 3 if audio.id3_prefix.version[1] == 3 else 4
                prefix = ID3(temp_name, v2_version=version)
                native_cover = next((picture for picture in audio.pictures if picture.type == 3), audio.pictures[0] if audio.pictures else None)
                prefix_cover = selected_cover(prefix)
                update_prefix_cover = cover_changed and (not native_cover or (prefix_cover and prefix_cover.data == native_cover.data))
                update_id3(prefix, changes, update_prefix_cover, cover_data, version)
                prefix.save(temp_name, v2_version=version)
                audio = load_audio(temp_name)
                logger.info("tags.id3_prefix_updated operation_id=%s file=%s version=%s", operation_id, path, version)
            if audio.tags is None:
                audio.add_tags()
            update_values(audio, changes)
            if cover_changed:
                update_cover(audio, cover_data)
            audio.save(temp_name)
        logger.info("tags.write_completed operation_id=%s file=%s format=%s elapsed_ms=%s", operation_id, path, type(audio).__name__, round((time.monotonic() - started) * 1000))
        if file_version(path) != expected_version:
            raise ValueError("文件在保存期间被其他程序修改，请重新加载后再保存。")
        os.replace(temp_name, path)
        temp_name = None
        logger.info("tags.save_completed operation_id=%s file=%s elapsed_ms=%s", operation_id, path, round((time.monotonic() - started) * 1000))
    except Exception:
        logger.exception("tags.save_failed operation_id=%s file=%s elapsed_ms=%s", operation_id, path, round((time.monotonic() - started) * 1000))
        raise
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
