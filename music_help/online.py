"""Search public music catalogues for metadata, artwork and lyrics."""

import base64
import json
import logging
import re
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen

import truststore

from . import subtitles

logger = logging.getLogger(__name__)
USER_AGENT = "MusicHelp/1.0 (personal music tag editor)"
SUBTITLE_API = "https://api.thesubtitledb.org"
SUBTITLE_MAX_RESULTS = 15
_SUBTITLE_ID = re.compile(r"[0-9]{1,20}")
_SUBTITLE_LANGUAGE_ORDER = {"en": 0, "zh": 1, "zt": 1}
ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_musicbrainz_lock = threading.Lock()
_last_musicbrainz_request = 0.0
_sensitive_key = re.compile(r"password|token|authorization|(?:^|[_-])auth(?:$|[_-])|cookie|secret|credential|session|signature|(?:^|[_-])(?:key|sign)$|api.?key|mp3url|rurl|rturl", re.I)


def _safe_log_value(value):
    if isinstance(value, dict):
        return {key: "<redacted>" if _sensitive_key.search(str(key)) else _safe_log_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_log_value(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
    return value


def _safe_target(url):
    parts = urlsplit(url)
    query = [(key, "<redacted>" if _sensitive_key.search(key) else value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, urlencode(query), ""))


def _safe_error_body(error):
    data = error.read(8192)
    try:
        return json.dumps(_safe_log_value(json.loads(data)), ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        return f"<non-JSON response {len(data)} bytes>"


def _safe_netease_image_url(url):
    if not isinstance(url, str):
        raise ValueError("无效的封面地址")
    parts = urlsplit(url)
    try:
        valid = (parts.scheme in {"http", "https"} and parts.hostname and re.fullmatch(r"(?:[a-z0-9-]+\.)*music\.126\.net", parts.hostname)
                 and not parts.username and not parts.password and parts.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("不允许的封面地址")
    return urlunsplit(("https", parts.hostname, parts.path, parts.query, ""))


class _NeteaseCoverRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _safe_netease_image_url(newurl))


def _open_request(url, restricted_cover):
    global _last_musicbrainz_request
    request = Request(url, headers={"User-Agent": USER_AGENT})
    if restricted_cover:
        opener = build_opener(HTTPSHandler(context=ssl_context), _NeteaseCoverRedirect())
        return opener.open(request, timeout=15)
    if url.startswith("https://musicbrainz.org/"):
        with _musicbrainz_lock:
            delay = 1.1 - (time.monotonic() - _last_musicbrainz_request)
            if delay > 0:
                time.sleep(delay)
            _last_musicbrainz_request = time.monotonic()
            return urlopen(request, timeout=15, context=ssl_context)
    return urlopen(request, timeout=15, context=ssl_context)


def fetch(url, operation_id, binary=False, restricted_cover=False):
    started = time.monotonic()
    target = _safe_target(url)
    logger.info("online.request_started operation_id=%s target=%s method=GET", operation_id, target)
    try:
        for attempt in range(2):
            try:
                response = _open_request(url, restricted_cover)
                break
            except HTTPError as exc:
                if not url.startswith("https://musicbrainz.org/") or exc.code not in (502, 503, 504) or attempt:
                    raise
                logger.warning("online.request_retry operation_id=%s target=%s attempt=%s status=%s response=%s elapsed_ms=%s", operation_id, target, attempt + 1, exc.code, _safe_error_body(exc), round((time.monotonic() - started) * 1000), exc_info=True)
                exc.close()
            except (URLError, TimeoutError, ssl.SSLError) as exc:
                if not url.startswith("https://musicbrainz.org/") or attempt:
                    raise
                logger.warning("online.request_retry operation_id=%s target=%s attempt=%s error=%s elapsed_ms=%s", operation_id, target, attempt + 1, type(exc).__name__, round((time.monotonic() - started) * 1000), exc_info=True)
            time.sleep(0.5)
        with response:
            data = response.read(10 * 1024 * 1024 + 1)
            if len(data) > 10 * 1024 * 1024:
                raise ValueError("在线响应超过大小限制")
            content_type = response.headers.get_content_type()
            result = None if binary else json.loads(data)
            body = f"<binary {len(data)} bytes, {content_type}>" if binary else json.dumps(_safe_log_value(result), ensure_ascii=False)
            logger.info("online.request_completed operation_id=%s target=%s status=%s response=%s elapsed_ms=%s", operation_id, target, response.status, body, round((time.monotonic() - started) * 1000))
            return (data, content_type) if binary else result
    except HTTPError as exc:
        logger.warning("online.request_failed operation_id=%s target=%s status=%s response=%s elapsed_ms=%s", operation_id, target, exc.code, _safe_error_body(exc), round((time.monotonic() - started) * 1000), exc_info=True)
        raise
    except Exception:
        logger.exception("online.request_failed operation_id=%s target=%s elapsed_ms=%s", operation_id, target, round((time.monotonic() - started) * 1000))
        raise


def _musicbrainz_query(title, artist, fields):
    def quote(value):
        return value.replace("\\", "\\\\").replace('"', '\\"')

    clauses = []
    if title:
        clauses.append("(" + " OR ".join(f'{field}:"{quote(title)}"' for field in fields) + ")")
    if artist:
        clauses.append(f'artist:"{quote(artist)}"')
    return " AND ".join(clauses)


def _artists(credit):
    return "".join((part.get("name") or "") + (part.get("joinphrase") or "") for part in credit if isinstance(part, dict))


def musicbrainz_search(title, artist, operation_id):
    query = _musicbrainz_query(title, artist, ("recording", "alias"))
    url = "https://musicbrainz.org/ws/2/recording/?" + urlencode({"query": query, "fmt": "json", "limit": 15})
    response = fetch(url, operation_id)
    results = []
    for recording in response.get("recordings", []):
        release = next(iter(recording.get("releases") or []), {})
        results.append({
            "source": "MusicBrainz · 歌曲", "kind": "recording",
            "title": recording.get("title") or "", "artist": _artists(recording.get("artist-credit") or []),
            "album": release.get("title") or "", "year": (release.get("date") or "")[:4],
            "duration": round((recording.get("length") or 0) / 1000), "releaseId": release.get("id") or "",
        })
    return results


def musicbrainz_release_search(title, artist, operation_id):
    query = _musicbrainz_query(title, artist, ("releasegroup", "alias", "release"))
    url = "https://musicbrainz.org/ws/2/release-group/?" + urlencode({"query": query, "fmt": "json", "limit": 15})
    response = fetch(url, operation_id)
    return [{
        "source": "MusicBrainz · 专辑", "kind": "release",
        "title": item.get("title") or "", "artist": _artists(item.get("artist-credit") or []),
        "album": item.get("title") or "", "year": (item.get("first-release-date") or "")[:4],
        "duration": 0, "releaseGroupId": item.get("id") or "",
    } for item in response.get("release-groups", [])]


def _netease_year(timestamp):
    try:
        return str(datetime.fromtimestamp(timestamp / 1000, timezone.utc).year) if timestamp else ""
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def netease_search(title, artist, operation_id, albums=False):
    params = {"s": " ".join(part for part in (title, artist) if part), "type": 10 if albums else 1, "limit": 15, "offset": 0}
    response = fetch("https://music.163.com/api/search/get/web?" + urlencode(params), operation_id)
    if response.get("code") != 200 or not isinstance(response.get("result"), dict):
        raise ValueError("网易云音乐搜索响应无效")
    items = response["result"].get("albums" if albums else "songs") or []
    candidates = []
    for item in items:
        album = item if albums else item.get("album") or {}
        candidate = {
            "source": "网易云音乐 · 专辑" if albums else "网易云音乐 · 歌曲",
            "kind": "release" if albums else "recording",
            "title": item.get("name") or "", "artist": "、".join(part.get("name") or "" for part in item.get("artists") or []),
            "album": album.get("name") or "", "year": _netease_year(album.get("publishTime")),
            "duration": 0 if albums else round((item.get("duration") or 0) / 1000),
        }
        identifier = item.get("id")
        if identifier is not None and re.fullmatch(r"[0-9]{1,20}", str(identifier)):
            candidate["neteaseAlbumId" if albums else "neteaseId"] = str(identifier)
        candidates.append(candidate)
    return candidates


def lrclib_search(title, artist, operation_id):
    params = {"track_name": title} if title else {"q": artist}
    if title and artist:
        params["artist_name"] = artist
    records = fetch("https://lrclib.net/api/search?" + urlencode(params), operation_id)
    return [{
        "source": "LRCLIB · 歌曲", "kind": "recording", "title": item.get("trackName") or "",
        "artist": item.get("artistName") or "", "album": item.get("albumName") or "", "year": "",
        "duration": round(item.get("duration") or 0), "lyricsId": item.get("id"), "releaseId": "",
    } for item in records]


def subtitle_search(title, artist, operation_id):
    query = title or artist
    if not query:
        return []
    url = f"{SUBTITLE_API}/v1/by-title?" + urlencode({"q": query, "limit": 50})
    response = fetch(url, operation_id)
    if not isinstance(response, dict) or response.get("error"):
        return []
    film = response.get("title") if isinstance(response.get("title"), dict) else {}
    items = (response.get("subtitles") or {}).get("items") or []
    candidates = []
    for item in items:
        subtitle_id, duration, cue_count = item.get("id"), item.get("duration_s"), item.get("cues")
        format_name = str(item.get("format") or "").lower()
        if format_name and format_name not in {"srt", "ass", "ssa", "vtt"}:
            continue
        if not isinstance(subtitle_id, int) or not isinstance(duration, (int, float)) or duration <= 0:
            continue
        if not isinstance(cue_count, int) or cue_count < duration / 60:
            continue
        candidates.append({
            "source": "TheSubtitleDB · 字幕", "kind": "subtitle",
            "title": film.get("name") or query, "artist": "", "album": "",
            "year": str(film.get("year") or ""), "duration": round(duration),
            "subtitleId": str(subtitle_id), "language": str(item.get("language") or ""),
            "format": str(item.get("format") or ""), "encoding": str(item.get("encoding") or ""),
            "releaseName": str(item.get("release_name") or "")[:300], "cues": cue_count,
        })
    candidates.sort(key=lambda candidate: (_SUBTITLE_LANGUAGE_ORDER.get(candidate["language"], 2), -candidate["duration"]))
    return candidates[:SUBTITLE_MAX_RESULTS]


def search(title, artist, operation_id, source=None):
    sources = {
        "netease_song": ("网易云音乐歌曲", lambda: netease_search(title, artist, operation_id)),
        "netease_album": ("网易云音乐专辑", lambda: netease_search(title, artist, operation_id, albums=True)),
        "musicbrainz": ("MusicBrainz 歌曲", lambda: musicbrainz_search(title, artist, operation_id)),
        "musicbrainz_release": ("MusicBrainz 专辑", lambda: musicbrainz_release_search(title, artist, operation_id)),
        "lrclib": ("LRCLIB", lambda: lrclib_search(title, artist, operation_id)),
        "subtitle": ("TheSubtitleDB 字幕", lambda: subtitle_search(title, artist, operation_id)),
    }
    if source == "subtitle":
        selected = {"subtitle": sources["subtitle"]}
    elif source:
        selected = {source: sources[source]}
    else:
        selected = {key: value for key, value in sources.items() if key != "subtitle"}
    logger.info("online.search_started operation_id=%s title=%s artist=%s sources=%s", operation_id, title, artist, list(selected))
    warnings = []
    results = {}

    def search_source(key, call):
        started = time.monotonic()
        try:
            result = call()
            logger.info("online.source_completed operation_id=%s source=%s result_count=%s elapsed_ms=%s", operation_id, key, len(result), round((time.monotonic() - started) * 1000))
            return result
        except Exception:
            logger.exception("online.source_failed operation_id=%s source=%s title=%s artist=%s elapsed_ms=%s", operation_id, key, title, artist, round((time.monotonic() - started) * 1000))
            raise

    with ThreadPoolExecutor(max_workers=len(selected)) as executor:
        futures = {key: executor.submit(search_source, key, call) for key, (_, call) in selected.items()}
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except Exception:
                results[key] = []
                warnings.append(f"{selected[key][0]} 暂时不可用")
    for lyric in results.get("lrclib", []):
        for recording in results.get("musicbrainz", []):
            if all(lyric[field].casefold() == recording[field].casefold() for field in ("title", "artist", "album")):
                lyric["releaseId"] = recording["releaseId"]
                lyric["year"] = recording["year"]
                break
    candidates = [candidate for key in selected for candidate in results[key]]
    logger.info("online.search_completed operation_id=%s result_count=%s warnings=%s", operation_id, len(candidates), warnings)
    return {"candidates": candidates, "warnings": warnings}


def _decode_subtitle(data, encoding):
    for name in (encoding, "utf-8-sig", "utf-8", "cp1252"):
        if not name:
            continue
        try:
            return data.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _subtitle_lyrics(candidate, operation_id):
    subtitle_id = candidate.get("subtitleId", "")
    if not isinstance(subtitle_id, str) or not _SUBTITLE_ID.fullmatch(subtitle_id):
        return None
    data, content_type = fetch(f"{SUBTITLE_API}/get/{subtitle_id}", operation_id, binary=True)
    if content_type not in ("application/x-subrip", "text/plain", "text/vtt", "application/octet-stream"):
        logger.warning("online.subtitle_unsupported operation_id=%s subtitle_id=%s content_type=%s", operation_id, subtitle_id, content_type)
        return None
    parsed = subtitles.cues(_decode_subtitle(data, candidate.get("encoding") or ""))
    if not parsed:
        logger.warning("online.subtitle_empty operation_id=%s subtitle_id=%s", operation_id, subtitle_id)
        return None
    lyrics = subtitles.to_lrc(parsed)
    logger.info("online.subtitle_converted operation_id=%s subtitle_id=%s cues=%s characters=%s", operation_id, subtitle_id, len(parsed), len(lyrics))
    return lyrics


def get_lyrics(candidate, operation_id):
    if candidate.get("kind") == "subtitle":
        return _subtitle_lyrics(candidate, operation_id)
    if candidate.get("kind") == "release" or candidate.get("skipLyrics"):
        return None
    netease_id = candidate.get("neteaseId", "")
    if isinstance(netease_id, str) and re.fullmatch(r"[0-9]{1,20}", netease_id):
        url = "https://music.163.com/api/song/lyric?" + urlencode({"id": netease_id, "lv": -1, "kv": -1, "tv": -1})
        result = fetch(url, operation_id)
        lyrics = (result.get("lrc") or {}).get("lyric")
        if lyrics:
            return lyrics
    lyrics_id = candidate.get("lyricsId")
    if isinstance(lyrics_id, int) and lyrics_id > 0:
        url = f"https://lrclib.net/api/get/{lyrics_id}"
    else:
        title, artist = candidate.get("title", ""), candidate.get("artist", "")
        duration = candidate.get("duration")
        if not title or not artist or not isinstance(duration, (int, float)) or duration <= 0:
            return None
        params = {"track_name": title, "artist_name": artist, "duration": round(duration)}
        if candidate.get("album"):
            params["album_name"] = candidate["album"]
        url = "https://lrclib.net/api/get?" + urlencode(params)
    try:
        result = fetch(url, operation_id)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return result.get("syncedLyrics") or result.get("plainLyrics") or None


def get_cover(candidate, operation_id):
    netease_id = candidate.get("neteaseId", "")
    album_id = candidate.get("neteaseAlbumId", "")
    if isinstance(netease_id, str) and re.fullmatch(r"[0-9]{1,20}", netease_id):
        url = "https://music.163.com/api/song/detail/?" + urlencode({"ids": f"[{netease_id}]"})
        result = fetch(url, operation_id)
        album = (next(iter(result.get("songs") or []), {}).get("album") or {})
        cover_url = album.get("picUrl")
    elif isinstance(album_id, str) and re.fullmatch(r"[0-9]{1,20}", album_id):
        result = fetch(f"https://music.163.com/api/album/{album_id}", operation_id)
        cover_url = (result.get("album") or {}).get("picUrl")
    else:
        cover_url = None
    if cover_url:
        data, content_type = fetch(_safe_netease_image_url(cover_url), operation_id, binary=True, restricted_cover=True)
    else:
        group_id = candidate.get("releaseGroupId", "")
        try:
            release_id = str(uuid.UUID(group_id or candidate.get("releaseId", "")))
        except (ValueError, TypeError, AttributeError):
            return None
        kind = "release-group" if group_id else "release"
        try:
            data, content_type = fetch(f"https://coverartarchive.org/{kind}/{release_id}/front-500", operation_id, binary=True)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type not in ("image/jpeg", "image/png"):
        return None
    return {"data": base64.b64encode(data).decode("ascii"), "mime": content_type}


def details(candidate, operation_id):
    logger.info("online.details_started operation_id=%s candidate=%s", operation_id, candidate)
    warnings = []
    result = {"lyrics": None, "cover": None}
    tasks = {}
    if not candidate.get("skipLyrics"):
        tasks["lyrics"] = get_lyrics
    if not candidate.get("skipCover"):
        tasks["cover"] = get_cover
    if tasks:
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = {field: executor.submit(call, candidate, operation_id) for field, call in tasks.items()}
            for field, future in futures.items():
                try:
                    result[field] = future.result()
                except Exception:
                    logger.exception("online.details_field_failed operation_id=%s field=%s candidate=%s", operation_id, field, candidate)
                    warnings.append("歌词获取失败" if field == "lyrics" else "封面获取失败")
    result["warnings"] = warnings
    logger.info("online.details_completed operation_id=%s lyrics_found=%s cover_found=%s warnings=%s", operation_id, bool(result["lyrics"]), bool(result["cover"]), warnings)
    return result
