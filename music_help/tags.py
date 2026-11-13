"""Read and safely update MP3 metadata."""

import base64
import binascii
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from mutagen.id3 import APIC, COMM, ID3, ID3NoHeaderError, TALB, TCOM, TCON, TDRC, TPE1, TPE2, TIT2, TPOS, TRCK, USLT
from mutagen.mp3 import MP3, HeaderNotFoundError

logger = logging.getLogger(__name__)

TEXT_FRAMES = {
    "title": TIT2,
    "artist": TPE1,
    "album": TALB,
    "albumArtist": TPE2,
    "year": TDRC,
    "track": TRCK,
    "disc": TPOS,
    "genre": TCON,
    "composer": TCOM,
}
FIELDS = set(TEXT_FRAMES) | {"comment", "lyrics"}
MAX_COVER_BYTES = 10 * 1024 * 1024


def file_version(path):
    stat = path.stat()
    return f"{stat.st_dev}-{stat.st_ino}-{stat.st_size}-{stat.st_mtime_ns}"


def selected_cover(tags):
    covers = tags.getall("APIC") if tags else []
    return next((cover for cover in covers if cover.type == 3), covers[0] if covers else None)


def read_track(path, name, source, relative_path=""):
    try:
        audio = MP3(path)
    except HeaderNotFoundError as exc:
        with open(path, "rb") as stream:
            header = stream.read(12)
        if header[4:8] == b"ftyp":
            raise ValueError("文件实际是 MP4 容器，无法作为 MP3 读取。") from exc
        raise ValueError("文件中没有找到有效的 MP3 音频帧。") from exc

    tags = audio.tags or ID3()
    values = {}
    for field, frame_type in TEXT_FRAMES.items():
        frame = tags.get(frame_type.__name__)
        values[field] = str(frame) if frame else ""
    for field, frame_id in (("comment", "COMM"), ("lyrics", "USLT")):
        frames = tags.getall(frame_id)
        values[field] = str(frames[0]) if frames else ""
    return {
        "name": name,
        "source": source,
        "relativePath": relative_path,
        "version": file_version(path),
        "size": path.stat().st_size,
        "duration": round(audio.info.length),
        "bitrate": round(audio.info.bitrate / 1000),
        "hasCover": selected_cover(tags) is not None,
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
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", data
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", data
    raise ValueError("封面仅支持 JPEG 或 PNG 图片。")


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
        handle, temp_name = tempfile.mkstemp(prefix=".music-help-", suffix=".mp3", dir=path.parent)
        os.close(handle)
        shutil.copy2(path, temp_name)
        logger.info("tags.copy_completed operation_id=%s file=%s bytes=%s elapsed_ms=%s", operation_id, path, path.stat().st_size, round((time.monotonic() - started) * 1000))
        with open(temp_name, "rb") as stream:
            header = stream.read(4)
        version = 3 if header[:3] == b"ID3" and header[3] == 3 else 4
        try:
            tags = ID3(temp_name, v2_version=version)
        except ID3NoHeaderError:
            tags = ID3()
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
        tags.save(temp_name, v2_version=version)
        logger.info("tags.write_completed operation_id=%s file=%s version=%s elapsed_ms=%s", operation_id, path, version, round((time.monotonic() - started) * 1000))
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
