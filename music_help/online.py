"""Search public music catalogues for metadata, artwork and lyrics."""

import base64
import json
import logging
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import truststore

logger = logging.getLogger(__name__)
USER_AGENT = "MusicHelp/1.0 (personal music tag editor)"
ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_musicbrainz_lock = threading.Lock()
_last_musicbrainz_request = 0.0


def fetch(url, operation_id, binary=False):
    global _last_musicbrainz_request
    started = time.monotonic()
    logger.info("online.request_started operation_id=%s target=%s method=GET", operation_id, url)
    try:
        if url.startswith("https://musicbrainz.org/"):
            with _musicbrainz_lock:
                delay = 1.1 - (time.monotonic() - _last_musicbrainz_request)
                if delay > 0:
                    time.sleep(delay)
                _last_musicbrainz_request = time.monotonic()
                response = urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=15, context=ssl_context)
        else:
            response = urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=15, context=ssl_context)
        with response:
            data = response.read(10 * 1024 * 1024 + 1)
            if len(data) > 10 * 1024 * 1024:
                raise ValueError("在线响应超过大小限制")
            content_type = response.headers.get_content_type()
            body = f"<binary {len(data)} bytes, {content_type}>" if binary else data.decode("utf-8", "replace")
            logger.info("online.request_completed operation_id=%s target=%s status=%s response=%s elapsed_ms=%s", operation_id, url, response.status, body, round((time.monotonic() - started) * 1000))
            return (data, content_type) if binary else json.loads(data)
    except HTTPError as exc:
        body = exc.read(8192).decode("utf-8", "replace")
        logger.warning("online.request_failed operation_id=%s target=%s status=%s response=%s elapsed_ms=%s", operation_id, url, exc.code, body, round((time.monotonic() - started) * 1000), exc_info=True)
        raise
    except Exception:
        logger.exception("online.request_failed operation_id=%s target=%s elapsed_ms=%s", operation_id, url, round((time.monotonic() - started) * 1000))
        raise


def musicbrainz_search(title, artist, operation_id):
    def quote(value):
        return value.replace("\\", "\\\\").replace('"', '\\"')

    query = f'recording:"{quote(title)}"'
    if artist:
        query += f' AND artist:"{quote(artist)}"'
    url = "https://musicbrainz.org/ws/2/recording/?" + urlencode({"query": query, "fmt": "json", "limit": 15})
    response = fetch(url, operation_id)
    results = []
    for recording in response.get("recordings", []):
        release = next(iter(recording.get("releases") or []), {})
        artists = "".join((part.get("name") or "") + (part.get("joinphrase") or "") for part in recording.get("artist-credit", []))
        results.append({
            "source": "MusicBrainz",
            "title": recording.get("title") or "",
            "artist": artists,
            "album": release.get("title") or "",
            "year": (release.get("date") or "")[:4],
            "duration": round((recording.get("length") or 0) / 1000),
            "releaseId": release.get("id") or "",
        })
    return results


def lrclib_search(title, artist, operation_id):
    params = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    records = fetch("https://lrclib.net/api/search?" + urlencode(params), operation_id)
    return [{
        "source": "LRCLIB",
        "title": item.get("trackName") or "",
        "artist": item.get("artistName") or "",
        "album": item.get("albumName") or "",
        "year": "",
        "duration": round(item.get("duration") or 0),
        "lyricsId": item.get("id"),
        "releaseId": "",
    } for item in records]


def search(title, artist, operation_id):
    logger.info("online.search_started operation_id=%s title=%s artist=%s", operation_id, title, artist)
    warnings = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            "MusicBrainz": executor.submit(musicbrainz_search, title, artist, operation_id),
            "LRCLIB": executor.submit(lrclib_search, title, artist, operation_id),
        }
        results = {}
        for source, future in futures.items():
            try:
                results[source] = future.result()
            except Exception:
                results[source] = []
                warnings.append(f"{source} 暂时不可用")
    for lyric in results["LRCLIB"]:
        for recording in results["MusicBrainz"]:
            if all(lyric[field].casefold() == recording[field].casefold() for field in ("title", "artist", "album")):
                lyric["releaseId"] = recording["releaseId"]
                lyric["year"] = recording["year"]
                break
    candidates = results["MusicBrainz"] + results["LRCLIB"]
    logger.info("online.search_completed operation_id=%s result_count=%s warnings=%s", operation_id, len(candidates), warnings)
    return {"candidates": candidates, "warnings": warnings}


def get_lyrics(candidate, operation_id):
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
    try:
        release_id = str(uuid.UUID(candidate.get("releaseId", "")))
    except (ValueError, TypeError, AttributeError):
        return None
    try:
        data, content_type = fetch(f"https://coverartarchive.org/release/{release_id}/front-500", operation_id, binary=True)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if content_type not in ("image/jpeg", "image/png"):
        return None
    return {"data": base64.b64encode(data).decode("ascii"), "mime": content_type}


def details(candidate, operation_id):
    logger.info("online.details_started operation_id=%s candidate=%s", operation_id, candidate)
    warnings = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            "lyrics": executor.submit(get_lyrics, candidate, operation_id),
            "cover": executor.submit(get_cover, candidate, operation_id),
        }
        result = {}
        for field, future in futures.items():
            try:
                result[field] = future.result()
            except Exception:
                result[field] = None
                warnings.append("歌词获取失败" if field == "lyrics" else "封面获取失败")
    result["warnings"] = warnings
    logger.info("online.details_completed operation_id=%s lyrics_found=%s cover_found=%s warnings=%s", operation_id, bool(result["lyrics"]), bool(result["cover"]), warnings)
    return result
