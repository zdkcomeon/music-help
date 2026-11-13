import argparse
import logging
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from music_help import online
from music_help.tags import AUDIO_EXTENSIONS, FIELDS, UnrecognizedAudio, audio_cover, file_version, load_audio, read_track, update_tags

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("music_help")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 12 * 1024 * 1024
csrf_token = uuid.uuid4().hex
uploads = tempfile.TemporaryDirectory(prefix="music-help-")
lock = threading.RLock()
entries = {}
library_root = None


@dataclass
class Entry:
    path: Path
    name: str
    source: str
    relative_path: str = ""


def valid_entry(file_id):
    entry = entries.get(file_id)
    if not entry:
        abort(404, "文件不存在或服务已重启，请重新导入。")
    if entry.path.is_symlink() or not entry.path.is_file():
        abort(404, "文件不存在或已被移动。")
    root = library_root if entry.source == "library" else Path(uploads.name)
    if root is None or not entry.path.resolve().is_relative_to(root.resolve()):
        abort(403, "文件路径超出允许的目录。")
    return entry


def track_data(file_id, entry):
    data = read_track(entry.path, entry.name, entry.source, entry.relative_path)
    data["id"] = file_id
    return data


def safe_name(name):
    name = str(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
    return name or "untitled"


def renamed_name(template, data, use_detected_extension=False):
    allowed = FIELDS | {"filename"}
    fields = re.findall(r"%([a-zA-Z]+)%", template)
    if not fields or any(field not in allowed for field in fields) or re.sub(r"%[a-zA-Z]+%", "", template).find("%") >= 0:
        raise ValueError("重命名模板包含无效字段。")
    values = {**data, "filename": data["baseName"]}
    name = re.sub(r"%([a-zA-Z]+)%", lambda match: str(values.get(match[1], "")), template)
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
    original_extension = data["fileExtension"]
    extension = data["detectedExtension"] if use_detected_extension else original_extension
    for suffix in {original_extension, extension} - {""}:
        if name.lower().endswith(suffix.lower()):
            name = name[:-len(suffix)].rstrip(" .")
            break
    if not name or name.casefold() in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}:
        raise ValueError("生成的文件名无效。")
    if len((name + extension).encode("utf-8")) > 240:
        raise ValueError("生成的文件名过长。")
    return name + extension


@app.before_request
def check_request():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("X-Music-Help-Token") != csrf_token:
        abort(403, "请求已过期，请刷新页面。")


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def client_error(error):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description), error.code
    return error


@app.get("/")
def index():
    return render_template("index.html", csrf_token=csrf_token, audio_extensions=sorted(AUDIO_EXTENSIONS))


@app.get("/api/library")
def library():
    operation_id = uuid.uuid4().hex
    started = time.monotonic()
    logger.info("library.scan_started operation_id=%s root=%s", operation_id, library_root)
    if library_root is None:
        return jsonify(configured=False, tracks=[], errors=[])
    tracks, errors = [], []
    def scan_error(exc):
        errors.append({"name": exc.filename or str(library_root), "error": str(exc)})
        logger.error("library.scan_failed operation_id=%s path=%s error=%s", operation_id, exc.filename, exc, exc_info=(type(exc), exc, exc.__traceback__))

    with lock:
        known = {entry.path: file_id for file_id, entry in entries.items() if entry.source == "library"}
        seen = set()
        for folder, directories, files in os.walk(library_root, followlinks=False, onerror=scan_error):
            directories[:] = sorted(name for name in directories if not (Path(folder) / name).is_symlink())
            for name in sorted(files):
                path = Path(folder) / name
                if path.is_symlink() or name.startswith(".music-help-"):
                    continue
                relative = str(path.relative_to(library_root))
                if not path.resolve().is_relative_to(library_root):
                    continue
                file_id = known.get(path, uuid.uuid4().hex)
                entry = Entry(path=path, name=name, source="library", relative_path=relative)
                try:
                    data = track_data(file_id, entry)
                    entries[file_id] = entry
                    seen.add(file_id)
                    tracks.append(data)
                    logger.info("library.file_loaded operation_id=%s file_id=%s path=%s format=%s status=success", operation_id, file_id, relative, data["format"])
                except UnrecognizedAudio as exc:
                    if path.suffix.lower() in AUDIO_EXTENSIONS:
                        errors.append({"name": relative, "error": str(exc)})
                        logger.warning("library.file_unrecognized operation_id=%s path=%s error=%s", operation_id, relative, exc)
                except Exception as exc:
                    errors.append({"name": relative, "error": str(exc)})
                    logger.exception("library.file_failed operation_id=%s path=%s", operation_id, relative)
        for file_id in set(known.values()) - seen:
            entries.pop(file_id, None)
    logger.info("library.scan_completed operation_id=%s root=%s count=%s errors=%s elapsed_ms=%s", operation_id, library_root, len(tracks), len(errors), round((time.monotonic() - started) * 1000))
    return jsonify(configured=True, tracks=tracks, errors=errors)


@app.post("/api/files")
def upload_file():
    operation_id = uuid.uuid4().hex
    started = time.monotonic()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        abort(400, "请选择音频文件。")
    name = safe_name(upload.filename)
    file_id = uuid.uuid4().hex
    path = Path(uploads.name) / file_id
    logger.info("upload.received operation_id=%s file_id=%s name=%s", operation_id, file_id, name)
    try:
        upload.save(path)
        entry = Entry(path=path, name=name, source="upload")
        data = track_data(file_id, entry)
        with lock:
            entries[file_id] = entry
        logger.info("upload.completed operation_id=%s file_id=%s format=%s bytes=%s elapsed_ms=%s", operation_id, file_id, data["format"], data["size"], round((time.monotonic() - started) * 1000))
        return jsonify(track=data)
    except Exception as exc:
        path.unlink(missing_ok=True)
        logger.exception("upload.failed operation_id=%s file_id=%s name=%s elapsed_ms=%s", operation_id, file_id, name, round((time.monotonic() - started) * 1000))
        return jsonify(error=str(exc)), 400


@app.delete("/api/files/<file_id>")
def remove_file(file_id):
    with lock:
        entry = valid_entry(file_id)
        entries.pop(file_id, None)
        if entry.source == "upload":
            entry.path.unlink(missing_ok=True)
    return jsonify(ok=True)


@app.get("/api/files/<file_id>")
def get_file(file_id):
    with lock:
        return jsonify(track=track_data(file_id, valid_entry(file_id)))


@app.post("/api/tags")
def save_tags():
    operation_id = uuid.uuid4().hex
    payload = request.get_json(silent=True) or {}
    selected = payload.get("files")
    changes = payload.get("changes")
    if not isinstance(selected, list) or not selected or not isinstance(changes, dict):
        abort(400, "请选择文件并提供要修改的标签。")
    if not changes and "cover" not in payload:
        abort(400, "没有需要保存的修改。")
    logger.info("tags.request_received operation_id=%s file_count=%s fields=%s cover_changed=%s", operation_id, len(selected), list(changes), "cover" in payload)
    saved, errors = [], []
    with lock:
        for item in selected:
            file_id = item.get("id") if isinstance(item, dict) else None
            try:
                entry = valid_entry(file_id)
                update_tags(entry.path, item.get("version"), changes, "cover" in payload, payload.get("cover"), operation_id)
                saved.append(track_data(file_id, entry))
            except Exception as exc:
                logger.exception("tags.item_failed operation_id=%s file_id=%s", operation_id, file_id)
                errors.append({"id": file_id, "error": getattr(exc, "description", str(exc))})
    logger.info("tags.request_completed operation_id=%s success_count=%s error_count=%s", operation_id, len(saved), len(errors))
    return jsonify(tracks=saved, errors=errors)


@app.post("/api/rename")
def rename():
    operation_id = uuid.uuid4().hex
    started = time.monotonic()
    payload = request.get_json(silent=True) or {}
    selected, template = payload.get("files"), payload.get("template")
    use_detected_extension = payload.get("useDetectedExtension", False)
    if not isinstance(selected, list) or not selected or not isinstance(template, str) or len(template) > 500 or not isinstance(use_detected_extension, bool):
        abort(400, "请选择文件并填写重命名模板。")
    logger.info("rename.request_received operation_id=%s file_count=%s template=%s use_detected_extension=%s", operation_id, len(selected), template, use_detected_extension)
    renamed, errors = [], []
    with lock:
        for item in selected:
            file_id = item.get("id") if isinstance(item, dict) else None
            item_started = time.monotonic()
            try:
                entry = valid_entry(file_id)
                if file_version(entry.path) != item.get("version"):
                    raise ValueError("文件已被其他程序修改，请重新加载后再重命名。")
                name = renamed_name(template, track_data(file_id, entry), use_detected_extension)
                if entry.source == "library" and name != entry.name:
                    target = entry.path.with_name(name)
                    try:
                        os.link(entry.path, target)
                    except FileExistsError:
                        raise
                    except OSError:
                        logger.info("rename.link_unavailable operation_id=%s file_id=%s target=%s", operation_id, file_id, target)
                        with open(target, "xb"):
                            pass
                        try:
                            os.replace(entry.path, target)
                        except Exception:
                            target.unlink(missing_ok=True)
                            raise
                    else:
                        try:
                            entry.path.unlink()
                        except Exception:
                            target.unlink(missing_ok=True)
                            raise
                    entry.path = target
                    entry.relative_path = str(target.relative_to(library_root))
                entry.name = name
                renamed.append(track_data(file_id, entry))
                logger.info("rename.item_completed operation_id=%s file_id=%s name=%s elapsed_ms=%s", operation_id, file_id, name, round((time.monotonic() - item_started) * 1000))
            except Exception as exc:
                logger.exception("rename.item_failed operation_id=%s file_id=%s elapsed_ms=%s", operation_id, file_id, round((time.monotonic() - item_started) * 1000))
                message = "目标文件已存在。" if isinstance(exc, FileExistsError) else getattr(exc, "description", str(exc))
                errors.append({"id": file_id, "error": message})
    logger.info("rename.request_completed operation_id=%s success_count=%s error_count=%s elapsed_ms=%s", operation_id, len(renamed), len(errors), round((time.monotonic() - started) * 1000))
    return jsonify(tracks=renamed, errors=errors)


@app.get("/api/files/<file_id>/cover")
def cover(file_id):
    entry = valid_entry(file_id)
    image = audio_cover(load_audio(entry.path))
    if not image:
        abort(404, "文件没有封面。")
    from io import BytesIO
    return send_file(BytesIO(image[1]), mimetype=image[0], max_age=0)


@app.get("/api/files/<file_id>/audio")
def audio(file_id):
    entry = valid_entry(file_id)
    data = track_data(file_id, entry)
    return send_file(entry.path, mimetype=data["mimeType"], conditional=True)


@app.get("/api/download")
def download():
    operation_id = uuid.uuid4().hex
    started = time.monotonic()
    file_ids = list(dict.fromkeys(request.args.getlist("id")))
    if not file_ids:
        abort(400, "请选择文件。")
    logger.info("download.started operation_id=%s file_ids=%s count=%s", operation_id, file_ids, len(file_ids))
    with lock:
        files = [valid_entry(file_id) for file_id in file_ids]
        if len(files) == 1:
            logger.info("download.file_ready operation_id=%s file_id=%s bytes=%s elapsed_ms=%s", operation_id, file_ids[0], files[0].path.stat().st_size, round((time.monotonic() - started) * 1000))
            return send_file(files[0].path, as_attachment=True, download_name=files[0].name, conditional=True)
        archive = tempfile.TemporaryFile()
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
                names = set()
                for file_id, entry in zip(file_ids, files):
                    item_started = time.monotonic()
                    name = entry.name
                    number = 2
                    while name.casefold() in names:
                        name = f"{Path(entry.name).stem} ({number}){Path(entry.name).suffix}"
                        number += 1
                    names.add(name.casefold())
                    output.write(entry.path, arcname=name)
                    logger.info("download.archive_item_added operation_id=%s file_id=%s name=%s bytes=%s elapsed_ms=%s", operation_id, file_id, name, entry.path.stat().st_size, round((time.monotonic() - item_started) * 1000))
            archive.seek(0)
            response = send_file(archive, mimetype="application/zip", as_attachment=True, download_name="music-help.zip")
            response.call_on_close(archive.close)
            logger.info("download.archive_ready operation_id=%s count=%s elapsed_ms=%s", operation_id, len(files), round((time.monotonic() - started) * 1000))
            return response
        except Exception:
            logger.exception("download.archive_failed operation_id=%s file_ids=%s elapsed_ms=%s", operation_id, file_ids, round((time.monotonic() - started) * 1000))
            archive.close()
            raise


@app.get("/api/online/search")
def online_search():
    title, artist = request.args.get("title", "").strip(), request.args.get("artist", "").strip()
    if not title or len(title) > 300 or len(artist) > 300:
        abort(400, "请填写不超过 300 个字符的歌曲名称。")
    return jsonify(online.search(title, artist, uuid.uuid4().hex))


@app.post("/api/online/details")
def online_details():
    candidate = request.get_json(silent=True)
    if not isinstance(candidate, dict):
        abort(400, "匹配结果无效。")
    for key in ("title", "artist", "album", "releaseId"):
        if key in candidate and (not isinstance(candidate[key], str) or len(candidate[key]) > 300):
            abort(400, "匹配结果无效。")
    candidate = {key: candidate[key] for key in ("title", "artist", "album", "releaseId", "duration", "lyricsId") if key in candidate}
    return jsonify(online.details(candidate, uuid.uuid4().hex))


def main():
    global library_root
    parser = argparse.ArgumentParser(description="Music Help 音频标签管理")
    parser.add_argument("--music-dir", default=os.environ.get("MUSIC_DIR"), help="服务器音乐目录")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    if args.music_dir:
        library_root = Path(args.music_dir).expanduser().resolve()
        if not library_root.is_dir():
            parser.error("音乐目录不存在。")
    from waitress import serve
    logger.info("server.started host=%s port=%s music_dir=%s", args.host, args.port, library_root)
    serve(app, host=args.host, port=args.port, threads=8, max_request_body_size=2 * 1024 * 1024 * 1024)


if __name__ == "__main__":
    main()
