"""Turn film subtitles into LRC lyrics."""

import html
import re

_MUSIC_NOTE = re.compile("[\u2669-\u266f\U0001f3b5\U0001f3b6]")
_HTML_TAG = re.compile(r"<[^>]*>")
_COLOR_TAG = re.compile(r"<font\b[^>]*>", re.I)
_ASS_TAG = re.compile(r"\{\\[^}]*\}")
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_BRACKET_ONLY = re.compile(r"^[-–—\s]*(?:\[[^\]]*\]|\([^)]*\)|【[^】]*】)$")
_CREDIT = re.compile(r"(?i)opensubtitles|subscene|addic7ed|podnapisi|vod2smi|subtitles? (?:by|from)|translated by|synced by|corrected by|modif(?:y|ied) by|www\.|https?://")
_TIMESTAMP = re.compile(r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_MAX_CUES = 20000
_STYLED_SHARE = 0.8


def _seconds(match):
    hours, minutes, seconds, fraction = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction) / 10 ** len(fraction)


def clean_text(value):
    value = _ASS_TAG.sub("", value)
    value = _HTML_TAG.sub("", value)
    value = html.unescape(value)
    value = _ZERO_WIDTH.sub("", value)
    value = _MUSIC_NOTE.sub(" ", value)
    value = value.replace("\\N", " ").replace("\\n", " ")
    return " ".join(value.split()).strip()


def _usable(line):
    return bool(line) and not _BRACKET_ONLY.match(line) and not _CREDIT.search(line) and any(character.isalnum() for character in line)


def _parse_timed(text):
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if "-->" not in line:
                continue
            start, _, end = line.partition("-->")
            start_match, end_match = _TIMESTAMP.search(start), _TIMESTAMP.search(end)
            if start_match and end_match:
                cues.append((_seconds(start_match), " ".join(lines[index + 1:])))
            break
    return cues


def _parse_ass(text):
    cues = []
    in_events = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_events = stripped.lower() == "[events]"
            continue
        if not in_events or not stripped.lower().startswith("dialogue:"):
            continue
        fields = stripped.split(":", 1)[1].split(",", 9)
        if len(fields) < 10:
            continue
        start_match = _TIMESTAMP.match(fields[1].strip())
        if start_match:
            cues.append((_seconds(start_match), fields[9]))
    return cues


def cues(text):
    if not isinstance(text, str):
        return []
    body = text.lstrip("\ufeff")
    lowered = body.lower()
    parsed = _parse_ass(body) if "[events]" in lowered and "dialogue:" in lowered else _parse_timed(body)
    cleaned = []
    for seconds, raw in parsed[:_MAX_CUES]:
        line = clean_text(raw)
        if _usable(line):
            cleaned.append((max(0.0, seconds), line, bool(_COLOR_TAG.search(raw))))
    if cleaned and sum(1 for cue in cleaned if cue[2]) >= len(cleaned) * _STYLED_SHARE:
        cleaned = [cue for cue in cleaned if cue[2]]
    cleaned.sort(key=lambda cue: cue[0])
    merged = []
    for seconds, line, _ in cleaned:
        if merged and seconds == merged[-1][0]:
            merged[-1] = (seconds, f"{merged[-1][1]} {line}")
        elif merged and line == merged[-1][1] and seconds - merged[-1][0] < 5:
            continue
        else:
            merged.append((seconds, line))
    return merged


def to_lrc(cues):
    lines = []
    for seconds, text in cues:
        centiseconds = round(seconds * 100)
        minutes, rest = divmod(centiseconds, 6000)
        lines.append(f"[{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}] {text}")
    return "\n".join(lines)


def lyrics_from_subtitle(text):
    parsed = cues(text)
    return to_lrc(parsed) if parsed else None
