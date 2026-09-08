#!/usr/bin/env python3
"""Extract video metadata and transcripts into one agent-friendly JSON document.

A thin, dependable wrapper around yt-dlp that turns any supported video URL into
a single JSON object: title, uploader, dates, tags, chapters, timestamped
caption segments and a plain-text transcript.

The interesting part is not the extraction, it is the failure handling. Every
failure is classified (rate_limit / blocked / transient / fatal), retried with a
policy that matches the category, and recorded in an append-only incident log so
that "what actually failed" is a question with a real answer.

By Guilty Trex. MIT licensed.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone

TOOL_NAME = "video-metadata-extractor"
TOOL_VERSION = "1.0.0"

DEFAULT_INCIDENT_LOG = "incidents.jsonl"
DEFAULT_LANGUAGES = ("en",)

# Subtitle formats this tool can actually parse, best first.
PARSEABLE_SUBTITLE_EXTS = ("vtt", "srt")


# --------------------------------------------------------------------------
# Failure categories
# --------------------------------------------------------------------------

class Category:
    """How a failure should be treated, not what caused it."""

    RATE_LIMIT = "rate_limit"   # 429 / explicit throttling: back off, be patient
    BLOCKED = "blocked"         # bot check, captcha, login wall: needs credentials
    TRANSIENT = "transient"     # 5xx, timeouts, reset connections: retry
    FATAL = "fatal"             # private, removed, 404, bad URL: never retry

    ALL = (RATE_LIMIT, BLOCKED, TRANSIENT, FATAL)


class ExtractionError(Exception):
    """A failure that survived the retry policy."""

    def __init__(self, message, category, http_status=None, incident_id=None,
                 attempts=1, stage="metadata"):
        super().__init__(message)
        self.message = message
        self.category = category
        self.http_status = http_status
        self.incident_id = incident_id
        self.attempts = attempts
        self.stage = stage

    def to_dict(self):
        return {
            "category": self.category,
            "message": self.message,
            "http_status": self.http_status,
            "incident_id": self.incident_id,
            "attempts": self.attempts,
            "stage": self.stage,
        }


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------

_HTTP_STATUS_RE = re.compile(r"HTTP\s+Error\s+(\d{3})", re.I)
_STATUS_CODE_RE = re.compile(r"\bstatus[ _-]?code[: ]+(\d{3})\b", re.I)

# Ordered: the first pattern that matches wins. Fatal patterns are checked
# before "blocked" so that "Private video. Sign in if you've been granted
# access" is not mistaken for a generic login wall.
_PATTERNS = (
    # --- rate limiting / throttling -------------------------------------
    (r"http error 429", Category.RATE_LIMIT),
    (r"\b429\b", Category.RATE_LIMIT),
    (r"too many requests", Category.RATE_LIMIT),
    (r"rate[- ]?limit", Category.RATE_LIMIT),
    (r"throttl", Category.RATE_LIMIT),
    (r"slow down", Category.RATE_LIMIT),
    (r"try again (later|in a few)", Category.RATE_LIMIT),

    # --- fatal ------------------------------------------------------------
    (r"private video", Category.FATAL),
    (r"video is private", Category.FATAL),
    (r"has been removed", Category.FATAL),
    (r"video unavailable", Category.FATAL),
    (r"this video is (no longer )?(unavailable|not available)", Category.FATAL),
    (r"account associated with this video has been (terminated|closed)", Category.FATAL),
    (r"http error 404", Category.FATAL),
    (r"http error 410", Category.FATAL),
    (r"not a valid url", Category.FATAL),
    (r"unsupported url", Category.FATAL),
    (r"incomplete youtube id", Category.FATAL),
    (r"is not a valid", Category.FATAL),
    (r"no video formats found", Category.FATAL),
    (r"copyright", Category.FATAL),
    (r"not made this video available in your country", Category.FATAL),
    (r"blocked it (on copyright|in your country)", Category.FATAL),
    (r"geo[- ]?restrict", Category.FATAL),
    (r"certificate_verify_failed", Category.FATAL),
    (r"certificate verify failed", Category.FATAL),

    # --- blocked: a human credential problem, not a patience problem ------
    (r"sign in to confirm", Category.BLOCKED),
    (r"confirm you'?re not a bot", Category.BLOCKED),
    (r"not a bot", Category.BLOCKED),
    (r"captcha", Category.BLOCKED),
    (r"cookies", Category.BLOCKED),
    (r"log ?in required", Category.BLOCKED),
    (r"login required", Category.BLOCKED),
    (r"requires authentication", Category.BLOCKED),
    (r"members[- ]only", Category.BLOCKED),
    (r"premium members", Category.BLOCKED),
    (r"age[- ]?restrict", Category.BLOCKED),
    (r"confirm your age", Category.BLOCKED),
    (r"http error 401", Category.BLOCKED),
    (r"http error 403", Category.BLOCKED),
    (r"forbidden", Category.BLOCKED),
    (r"access denied", Category.BLOCKED),

    # --- transient --------------------------------------------------------
    (r"http error 5\d\d", Category.TRANSIENT),
    (r"timed out", Category.TRANSIENT),
    (r"timeout", Category.TRANSIENT),
    (r"connection reset", Category.TRANSIENT),
    (r"connection aborted", Category.TRANSIENT),
    (r"connection refused", Category.TRANSIENT),
    (r"remote end closed", Category.TRANSIENT),
    (r"temporary failure in name resolution", Category.TRANSIENT),
    (r"unable to download (webpage|video data|api page|json metadata)", Category.TRANSIENT),
    (r"read operation timed out", Category.TRANSIENT),
    (r"broken pipe", Category.TRANSIENT),
    (r"bad gateway", Category.TRANSIENT),
    (r"service unavailable", Category.TRANSIENT),
    (r"eof occurred", Category.TRANSIENT),
    (r"network is unreachable", Category.TRANSIENT),
)

_COMPILED_PATTERNS = tuple((re.compile(p, re.I), c) for p, c in _PATTERNS)


def error_message(error):
    """Flatten an exception (or a string) into a single-line message."""
    if isinstance(error, str):
        text = error
    else:
        text = str(error) or error.__class__.__name__
        cause = getattr(error, "__cause__", None) or getattr(error, "__context__", None)
        if cause is not None and str(cause) and str(cause) not in text:
            text = "%s: %s" % (text, cause)
    text = re.sub(r"^ERROR:\s*", "", text.strip())
    return re.sub(r"\s+", " ", text)


def http_status_from(error):
    """Best-effort HTTP status for an exception or message."""
    for obj in _exception_chain(error):
        for attr in ("status", "code", "http_status"):
            value = getattr(obj, attr, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return value
    match = _HTTP_STATUS_RE.search(error_message(error))
    if match:
        return int(match.group(1))
    match = _STATUS_CODE_RE.search(error_message(error))
    if match:
        return int(match.group(1))
    return None


def classify_error(error):
    """Return ``(category, http_status)`` for an exception or message string.

    Classification is deliberately message-driven: yt-dlp funnels almost
    everything through ``DownloadError`` and the human-readable text is the only
    reliable signal across extractors.
    """
    status = http_status_from(error)
    message = error_message(error)

    if status == 429:
        return Category.RATE_LIMIT, status

    for pattern, category in _COMPILED_PATTERNS:
        if pattern.search(message):
            return category, status

    if status is not None:
        if status == 429:
            return Category.RATE_LIMIT, status
        if status in (401, 403):
            return Category.BLOCKED, status
        if status in (404, 410):
            return Category.FATAL, status
        if 500 <= status <= 599:
            return Category.TRANSIENT, status

    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return Category.TRANSIENT, status

    # Unknown failures get one cautious retry rather than a silent give-up.
    return Category.TRANSIENT, status


def _exception_chain(error, limit=8):
    """Yield an exception and the exceptions it wraps (yt-dlp nests deeply)."""
    seen = []
    queue = [error]
    while queue and len(seen) < limit:
        current = queue.pop(0)
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        yield current
        for attr in ("__cause__", "__context__", "cause", "reason", "orig_exc"):
            queue.append(getattr(current, attr, None))
        exc_info = getattr(current, "exc_info", None)
        if isinstance(exc_info, tuple) and len(exc_info) == 3:
            queue.append(exc_info[1])


def extract_retry_after(error):
    """Pull a ``Retry-After`` value (seconds) out of an exception, if present."""
    for obj in _exception_chain(error):
        headers = getattr(obj, "headers", None)
        getter = getattr(headers, "get", None)
        if getter is None:
            continue
        try:
            raw = getter("Retry-After") or getter("retry-after")
        except Exception:
            raw = None
        seconds = _parse_retry_after(raw)
        if seconds is not None:
            return seconds
    match = re.search(r"retry[- ]after[:= ]+(\d+(?:\.\d+)?)", error_message(error), re.I)
    if match:
        return float(match.group(1))
    return None


def _parse_retry_after(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    # HTTP-date form.
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%A, %d-%b-%y %H:%M:%S %Z"):
        try:
            when = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    return None


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------

def compute_backoff(attempt, base=2.0, cap=300.0, retry_after=None, jitter=random.random):
    """Exponential backoff with full jitter, capped, honouring ``Retry-After``.

    ``attempt`` is 1-based: the delay after the first failed attempt uses
    ``base``. A server-supplied ``Retry-After`` wins outright (still capped) --
    guessing is worse than being told.
    """
    if attempt < 1:
        attempt = 1
    cap = max(0.0, float(cap))
    if retry_after is not None:
        return min(max(0.0, float(retry_after)), cap)
    exponential = float(base) * (2 ** (attempt - 1))
    ceiling = min(cap, exponential)
    if ceiling <= 0:
        return 0.0
    return ceiling * jitter()


# --------------------------------------------------------------------------
# WebVTT / SRT parsing
# --------------------------------------------------------------------------

_CUE_RE = re.compile(
    r"^\s*((?:\d{1,3}:)?\d{1,3}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"((?:\d{1,3}:)?\d{1,3}:\d{2}[.,]\d{1,3})"
)
_KARAOKE_TS_RE = re.compile(r"<\d{1,3}:\d{2}:\d{2}[.,]\d{1,3}>")
_TAG_RE = re.compile(r"</?[a-zA-Z!][^>\n]{0,120}>")
_BLOCK_KEYWORDS = ("NOTE", "STYLE", "REGION", "WEBVTT")


def parse_timestamp(value):
    """``00:01:02.500`` / ``01:02.500`` / ``00:01:02,500`` -> seconds (float)."""
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if not parts or len(parts) > 3:
        raise ValueError("bad timestamp: %r" % value)
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def clean_caption_text(line):
    """Strip karaoke tags and markup, unescape entities, collapse whitespace."""
    text = _KARAOKE_TS_RE.sub("", line)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_vtt(data):
    """Parse WebVTT (or SRT) into ``[{'start','end','lines'}]`` cues.

    Cue payloads are cleaned but *not* deduplicated -- see :func:`merge_cues`.
    """
    if not data:
        return []
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    data = data.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    lines = data.split("\n")

    cues = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.split(" ")[0] in _BLOCK_KEYWORDS and "-->" not in stripped:
            # Skip a metadata block wholesale (NOTE bodies may contain anything).
            index += 1
            while index < len(lines) and lines[index].strip() and not _CUE_RE.match(lines[index]):
                index += 1
            continue
        match = _CUE_RE.match(line)
        if not match:
            index += 1
            continue
        try:
            start = parse_timestamp(match.group(1))
            end = parse_timestamp(match.group(2))
        except ValueError:
            index += 1
            continue
        index += 1
        payload = []
        # A cue ends at a truly empty line or at the next cue header. YouTube
        # pads its automatic captions with whitespace-only lines, which are
        # part of the cue, not the end of it.
        while index < len(lines) and lines[index] != "" and not _CUE_RE.match(lines[index]):
            cleaned = clean_caption_text(lines[index])
            if cleaned:
                payload.append(cleaned)
            index += 1
        if payload:
            cues.append({"start": start, "end": end, "lines": payload})
    return cues


def strip_repeated_prefix(previous, current):
    """Remove text ``current`` already inherited from ``previous``.

    YouTube's automatic captions roll: each cue repeats the previous cue and
    appends a few words. Only whole-phrase repeats are stripped, so genuinely
    repeated words in ordinary speech survive.
    """
    if not previous or not current:
        return current
    if current == previous:
        return ""
    if current.startswith(previous + " "):
        return current[len(previous) + 1:].strip()
    return current


def merge_cues(cues):
    """Collapse rolling caption cues into non-repeating timestamped segments."""
    segments = []
    previous_lines = []
    previous_text = ""
    for cue in cues:
        lines = [line for line in cue.get("lines", []) if line]
        if not lines:
            continue
        raw_text = " ".join(lines)
        fresh = [line for line in lines if line not in previous_lines]
        text = strip_repeated_prefix(previous_text, " ".join(fresh)).strip()
        previous_lines = lines
        previous_text = raw_text
        if not text:
            continue
        start = round(float(cue["start"]), 3)
        end = round(float(cue["end"]), 3)
        if segments and segments[-1]["text"] == text:
            segments[-1]["end"] = max(segments[-1]["end"], end)
            continue
        segments.append({"start": start, "end": end, "text": text})
    return segments


def segments_to_transcript(segments):
    """Flatten segments into one plain-text transcript."""
    return re.sub(r"\s+", " ", " ".join(s["text"] for s in segments)).strip()


def parse_captions(data):
    """WebVTT/SRT text -> ``(segments, transcript)``."""
    segments = merge_cues(parse_vtt(data))
    return segments, segments_to_transcript(segments)


# --------------------------------------------------------------------------
# Language matching
# --------------------------------------------------------------------------

def normalize_language(code):
    """``en-US`` / ``en_GB`` / ``en-orig`` -> ``en``."""
    if not code:
        return ""
    return re.split(r"[-_]", str(code).strip().lower(), maxsplit=1)[0]


def languages_match(wanted, available):
    """Loose match: ``en`` matches ``en-US``; ``en-US`` matches ``en``."""
    if not wanted or not available:
        return False
    if str(wanted).lower() == str(available).lower():
        return True
    return normalize_language(wanted) == normalize_language(available)


def select_subtitle_track(info, languages=DEFAULT_LANGUAGES, allow_auto=True,
                          allow_any=False):
    """Choose the best caption track from a yt-dlp info dict.

    Manual subtitles beat automatic captions; an exact language tag beats a
    loose one. Returns ``{'language','source','ext','url'}`` or ``None``.
    """
    manual = info.get("subtitles") or {}
    automatic = (info.get("automatic_captions") or {}) if allow_auto else {}
    tables = (("manual", manual), ("automatic", automatic))

    def pick(source, table, code):
        track = _pick_parseable(table.get(code) or [])
        if track is None:
            return None
        return {"language": code, "source": source, "ext": track.get("ext"),
                "url": track.get("url")}

    for source, table in tables:
        for wanted in languages:
            for code in sorted(table):
                if str(code).lower() == str(wanted).lower():
                    found = pick(source, table, code)
                    if found:
                        return found
        for wanted in languages:
            for code in sorted(table):
                if languages_match(wanted, code):
                    found = pick(source, table, code)
                    if found:
                        return found
    if allow_any:
        for source, table in tables:
            for code in sorted(table):
                found = pick(source, table, code)
                if found:
                    return found
    return None


def _pick_parseable(tracks):
    for ext in PARSEABLE_SUBTITLE_EXTS:
        for track in tracks:
            if track.get("ext") == ext and track.get("url"):
                return track
    return None


# --------------------------------------------------------------------------
# Incident log
# --------------------------------------------------------------------------

def utcnow_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_incident_id():
    return uuid.uuid4().hex[:12]


class IncidentLogger:
    """Append-only JSONL log of every rate_limit / blocked / transient event.

    One line per attempt, plus a ``resolved: true`` line when a retry finally
    succeeds. Incidents with no resolution line are the real failure list.
    """

    FIELDS = ("timestamp", "tool", "tool_version", "incident_id", "url", "stage",
              "category", "http_status", "error", "attempt", "max_attempts",
              "retry_in_seconds", "resolved")

    def __init__(self, path=DEFAULT_INCIDENT_LOG, enabled=True, stream=None):
        self.path = path
        self.enabled = enabled and bool(path)
        self.stream = stream
        self.records = []
        self._warned = False

    def log(self, url, stage, category, attempt, max_attempts, incident_id,
            error=None, http_status=None, retry_in_seconds=None, resolved=False):
        record = {
            "timestamp": utcnow_iso(),
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "incident_id": incident_id,
            "url": url,
            "stage": stage,
            "category": category,
            "http_status": http_status,
            "error": error,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "retry_in_seconds": (round(float(retry_in_seconds), 3)
                                 if retry_in_seconds is not None else None),
            "resolved": bool(resolved),
        }
        self.records.append(record)
        self._write(record)
        return record

    def _write(self, record):
        line = json.dumps(record, ensure_ascii=False)
        if self.stream is not None:
            self.stream.write(line + "\n")
            flush = getattr(self.stream, "flush", None)
            if flush:
                flush()
            return
        if not self.enabled:
            return
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:  # never let logging break an extraction run
            if not self._warned:
                self._warned = True
                sys.stderr.write("warning: cannot write incident log %s: %s\n"
                                 % (self.path, exc))


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------

class RetryPolicy:
    """How many attempts each category gets, and how long to wait between them.

    A throttled request needs patience. A bot check needs credentials, so it
    gets a much shorter budget -- retrying it is mostly a way to get banned.
    """

    def __init__(self, retries=4, blocked_retries=1, base_backoff=2.0,
                 max_backoff=300.0):
        self.retries = max(0, int(retries))
        self.blocked_retries = max(0, int(blocked_retries))
        self.base_backoff = float(base_backoff)
        self.max_backoff = float(max_backoff)

    def max_attempts(self, category):
        if category == Category.FATAL:
            return 1
        if category == Category.BLOCKED:
            return self.blocked_retries + 1
        return self.retries + 1

    def delay(self, attempt, retry_after=None, jitter=random.random):
        return compute_backoff(attempt, self.base_backoff, self.max_backoff,
                               retry_after, jitter)


def run_with_retries(operation, url, stage, policy, incidents, sleeper=time.sleep,
                     jitter=random.random, on_retry=None):
    """Run ``operation``; retry per ``policy``; log every incident.

    Raises :class:`ExtractionError` when the budget is exhausted or the failure
    is fatal.
    """
    attempt = 0
    incident_id = None
    last_category = None
    last_status = None
    last_message = None
    last_max_attempts = 1

    while True:
        attempt += 1
        try:
            result = operation()
        except ExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
            category, status = classify_error(exc)
            message = error_message(exc)
            max_attempts = policy.max_attempts(category)
            will_retry = category != Category.FATAL and attempt < max_attempts
            retry_in = None
            if will_retry:
                retry_in = policy.delay(attempt, extract_retry_after(exc), jitter)
            if incident_id is None:
                incident_id = new_incident_id()
            last_category, last_status = category, status
            last_message, last_max_attempts = message, max_attempts
            incidents.log(url=url, stage=stage, category=category, attempt=attempt,
                          max_attempts=max_attempts, incident_id=incident_id,
                          error=message, http_status=status,
                          retry_in_seconds=retry_in, resolved=False)
            if not will_retry:
                raise ExtractionError(message, category, status, incident_id,
                                      attempt, stage)
            if on_retry is not None:
                on_retry(category, attempt, max_attempts, retry_in, message)
            sleeper(retry_in)
        else:
            if incident_id is not None:
                incidents.log(url=url, stage=stage, category=last_category,
                              attempt=attempt, max_attempts=last_max_attempts,
                              incident_id=incident_id, error=last_message,
                              http_status=last_status, retry_in_seconds=None,
                              resolved=True)
            return result


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class Config:
    """Everything the extractor needs, shared by the CLI and the library API."""

    def __init__(self, languages=DEFAULT_LANGUAGES, any_language=False,
                 subtitles=True, auto_subtitles=True, playlist=False,
                 playlist_items=None, max_entries=None, retries=4,
                 blocked_retries=1, base_backoff=2.0, max_backoff=300.0,
                 sleep_interval=0.0, socket_timeout=30.0, cookies=None,
                 cookies_from_browser=None, proxy=None, incident_log=DEFAULT_INCIDENT_LOG,
                 write_incidents=True, verbose=False):
        self.languages = tuple(languages) or DEFAULT_LANGUAGES
        self.any_language = bool(any_language)
        self.subtitles = bool(subtitles)
        self.auto_subtitles = bool(auto_subtitles)
        self.playlist = bool(playlist)
        self.playlist_items = playlist_items
        self.max_entries = max_entries
        self.sleep_interval = max(0.0, float(sleep_interval))
        self.socket_timeout = float(socket_timeout)
        self.cookies = cookies
        self.cookies_from_browser = cookies_from_browser
        self.proxy = proxy
        self.incident_log = incident_log
        self.write_incidents = bool(write_incidents)
        self.verbose = bool(verbose)
        self.policy = RetryPolicy(retries=retries, blocked_retries=blocked_retries,
                                  base_backoff=base_backoff, max_backoff=max_backoff)


_COOKIES_FROM_BROWSER_RE = re.compile(
    r"""(?x)
    (?P<name>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
    $"""
)


def parse_cookies_from_browser(value):
    """``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` -> yt-dlp's 4-tuple."""
    if not value:
        return None
    match = _COOKIES_FROM_BROWSER_RE.match(value.strip())
    if not match:
        raise ValueError("invalid --cookies-from-browser value: %r" % value)
    name = match.group("name").strip().lower()
    keyring = match.group("keyring")
    profile = match.group("profile")
    container = match.group("container")
    return (name,
            profile.strip() if profile else None,
            keyring.strip().upper() if keyring else None,
            container.strip() if container else None)


class _SilentLogger:
    """Swallow yt-dlp's own console output; this tool reports failures itself."""

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def build_ydl_options(config, flat=False):
    options = {
        "logger": _SilentLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "ignoreerrors": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        # Retries are this tool's job: yt-dlp's own loops would hide the
        # classification and the incident log.
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "socket_timeout": config.socket_timeout,
        "noplaylist": not config.playlist,
    }
    if flat:
        options["extract_flat"] = "in_playlist"
    if config.playlist_items:
        options["playlist_items"] = config.playlist_items
    if config.cookies:
        options["cookiefile"] = config.cookies
    if config.cookies_from_browser:
        options["cookiesfrombrowser"] = parse_cookies_from_browser(config.cookies_from_browser)
    if config.proxy:
        options["proxy"] = config.proxy
    if config.sleep_interval:
        options["sleep_interval_requests"] = config.sleep_interval
    return options


def default_ydl_factory(options):
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SystemExit("yt-dlp is not installed. Try: pip install yt-dlp") from exc
    return YoutubeDL(options)


# --------------------------------------------------------------------------
# Record building
# --------------------------------------------------------------------------

def format_upload_date(info):
    for key in ("upload_date", "release_date"):
        raw = info.get(key)
        if raw and re.fullmatch(r"\d{8}", str(raw)):
            raw = str(raw)
            return "%s-%s-%s" % (raw[:4], raw[4:6], raw[6:8])
    for key in ("timestamp", "release_timestamp"):
        raw = info.get(key)
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, timezone.utc).strftime("%Y-%m-%d")
    return None


def normalize_chapters(info):
    chapters = info.get("chapters") or []
    normalized = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        normalized.append({
            "start": _as_float(chapter.get("start_time", chapter.get("start"))),
            "end": _as_float(chapter.get("end_time", chapter.get("end"))),
            "title": chapter.get("title"),
        })
    return normalized


def _as_float(value):
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_record(info, source_url, segments=None, transcript=None,
                 transcript_source=None, transcript_language=None):
    """Turn a yt-dlp info dict into the stable output schema."""
    info = info or {}
    segments = segments or []
    tags = [str(tag) for tag in (info.get("tags") or []) if tag]
    categories = [str(item) for item in (info.get("categories") or []) if item]
    return {
        # --- core, stable ------------------------------------------------
        "title": info.get("title"),
        "source_url": source_url,
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "upload_date": format_upload_date(info),
        "duration_seconds": _as_int(info.get("duration")),
        "tags": tags,
        "timestamps": segments,
        "transcript": transcript or "",
        "license": info.get("license"),
        "extracted_at": utcnow_iso(),
        "tool_version": TOOL_VERSION,
        # --- additive agent context --------------------------------------
        "video_id": info.get("id"),
        "webpage_url": info.get("webpage_url") or source_url,
        "description": info.get("description"),
        "channel": info.get("channel"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "categories": categories,
        "view_count": _as_int(info.get("view_count")),
        "like_count": _as_int(info.get("like_count")),
        "thumbnail": info.get("thumbnail"),
        "language": info.get("language"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "chapters": normalize_chapters(info),
        "transcript_source": transcript_source,
        "transcript_language": transcript_language,
        "error": None,
    }


def build_error_record(source_url, error):
    """The inline failure record used in batch output (never aborts a run)."""
    return {
        "title": None,
        "source_url": source_url,
        "uploader": None,
        "upload_date": None,
        "duration_seconds": None,
        "tags": [],
        "timestamps": [],
        "transcript": "",
        "license": None,
        "extracted_at": utcnow_iso(),
        "tool_version": TOOL_VERSION,
        "video_id": None,
        "webpage_url": source_url,
        "description": None,
        "channel": None,
        "channel_url": None,
        "categories": [],
        "view_count": None,
        "like_count": None,
        "thumbnail": None,
        "language": None,
        "extractor": None,
        "chapters": [],
        "transcript_source": None,
        "transcript_language": None,
        "error": error.to_dict() if isinstance(error, ExtractionError) else {
            "category": Category.FATAL,
            "message": error_message(error),
            "http_status": http_status_from(error),
            "incident_id": None,
            "attempts": 1,
            "stage": "metadata",
        },
    }


# --------------------------------------------------------------------------
# Extractor
# --------------------------------------------------------------------------

class Extractor:
    """Extract one URL at a time, reusing a single yt-dlp session."""

    def __init__(self, config=None, incidents=None, ydl_factory=default_ydl_factory,
                 sleeper=time.sleep, jitter=random.random):
        self.config = config or Config()
        self.incidents = incidents or IncidentLogger(
            self.config.incident_log, enabled=self.config.write_incidents)
        self.ydl_factory = ydl_factory
        self.sleeper = sleeper
        self.jitter = jitter
        self._ydl = None
        self._flat_ydl = None

    # -- lifecycle -------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        for attribute in ("_ydl", "_flat_ydl"):
            ydl = getattr(self, attribute, None)
            closer = getattr(ydl, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:  # pragma: no cover - best effort
                    pass
            setattr(self, attribute, None)

    def _get_ydl(self, flat=False):
        attribute = "_flat_ydl" if flat else "_ydl"
        ydl = getattr(self, attribute)
        if ydl is None:
            ydl = self.ydl_factory(build_ydl_options(self.config, flat=flat))
            setattr(self, attribute, ydl)
        return ydl

    def _retry(self, operation, url, stage):
        return run_with_retries(operation, url=url, stage=stage,
                                policy=self.config.policy, incidents=self.incidents,
                                sleeper=self.sleeper, jitter=self.jitter,
                                on_retry=self._announce_retry if self.config.verbose else None)

    @staticmethod
    def _announce_retry(category, attempt, max_attempts, retry_in, message):
        sys.stderr.write("  %s (attempt %d/%d), retrying in %.1fs: %s\n"
                         % (category, attempt, max_attempts, retry_in, message[:160]))

    # -- public API ------------------------------------------------------
    def expand(self, url):
        """Expand a playlist/channel URL into individual video URLs."""
        ydl = self._get_ydl(flat=True)
        info = self._retry(lambda: ydl.extract_info(url, download=False),
                           url=url, stage="playlist")
        urls = list(_iter_entry_urls(info, url))
        if self.config.max_entries:
            urls = urls[:int(self.config.max_entries)]
        return urls

    def extract(self, url):
        """Extract one video. Raises :class:`ExtractionError` on failure."""
        ydl = self._get_ydl()
        info = self._retry(lambda: ydl.extract_info(url, download=False),
                           url=url, stage="metadata")
        info = _first_video(info)
        segments, transcript, source, language = [], "", None, None
        if self.config.subtitles:
            segments, transcript, source, language = self._fetch_transcript(info, url)
        return build_record(info, url, segments, transcript, source, language)

    # -- internals -------------------------------------------------------
    def _fetch_transcript(self, info, url):
        track = select_subtitle_track(info, self.config.languages,
                                      allow_auto=self.config.auto_subtitles,
                                      allow_any=self.config.any_language)
        if not track:
            return [], "", None, None
        ydl = self._get_ydl()

        def download():
            return ydl.urlopen(track["url"]).read()

        try:
            data = self._retry(download, url=url, stage="subtitles")
        except ExtractionError as exc:
            # Metadata already succeeded; a missing transcript is not a reason
            # to throw the whole record away.
            sys.stderr.write("warning: transcript unavailable for %s (%s): %s\n"
                             % (url, exc.category, exc.message))
            return [], "", None, None
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        segments, transcript = parse_captions(data)
        if not segments:
            return [], "", None, None
        return segments, transcript, track["source"], track["language"]


def _first_video(info):
    """Unwrap playlist results down to a single video info dict."""
    seen = 0
    while isinstance(info, dict) and info.get("_type") in ("playlist", "multi_video"):
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            break
        info = entries[0]
        seen += 1
        if seen > 8:  # pragma: no cover - pathological nesting
            break
    return info or {}


def _iter_entry_urls(info, fallback_url, depth=0):
    if not isinstance(info, dict) or depth > 6:
        return
    entries = info.get("entries")
    if entries is None:
        url = info.get("webpage_url") or info.get("url") or fallback_url
        if url:
            yield url
        return
    for entry in entries:
        if not entry:
            continue
        if entry.get("entries") is not None:
            for nested in _iter_entry_urls(entry, fallback_url, depth + 1):
                yield nested
            continue
        url = entry.get("url") or entry.get("webpage_url")
        if not url and entry.get("id") and entry.get("ie_key") == "Youtube":
            url = "https://www.youtube.com/watch?v=%s" % entry["id"]
        if url:
            yield url


# --------------------------------------------------------------------------
# Library API
# --------------------------------------------------------------------------

def extract_video(url, config=None, incidents=None, ydl_factory=default_ydl_factory,
                  **kwargs):
    """Extract a single URL and return the record. Raises ``ExtractionError``.

    >>> record = extract_video("https://archive.org/details/BigBuckBunny_124")
    >>> record["title"]
    """
    config = config or Config(**kwargs)
    with Extractor(config, incidents, ydl_factory) as extractor:
        return extractor.extract(url)


def extract_videos(urls, config=None, incidents=None, on_record=None,
                   ydl_factory=default_ydl_factory, **kwargs):
    """Extract many URLs. Failures become inline error records, not exceptions.

    Returns ``(records, failure_count)``.
    """
    config = config or Config(**kwargs)
    records = []
    failures = 0
    with Extractor(config, incidents, ydl_factory) as extractor:
        targets = list(urls)
        if config.playlist:
            expanded = []
            for url in targets:
                try:
                    expanded.extend(extractor.expand(url))
                except ExtractionError as exc:
                    failures += 1
                    record = build_error_record(url, exc)
                    records.append(record)
                    if on_record:
                        on_record(record)
            targets = expanded
        for index, url in enumerate(targets):
            if index and config.sleep_interval:
                time.sleep(config.sleep_interval)
            try:
                record = extractor.extract(url)
            except ExtractionError as exc:
                failures += 1
                record = build_error_record(url, exc)
            records.append(record)
            if on_record:
                on_record(record)
    return records, failures


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def read_batch_file(path):
    """One URL per line; blank lines and ``#`` comments ignored."""
    urls = []
    stream = sys.stdin if path == "-" else open(path, "r", encoding="utf-8")
    try:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line.split()[0])
    finally:
        if stream is not sys.stdin:
            stream.close()
    return urls


def build_parser():
    parser = argparse.ArgumentParser(
        prog="video-metadata-extractor",
        description="Extract video metadata and transcripts to agent-friendly JSON.",
        epilog="Example: video-metadata-extractor https://www.youtube.com/watch?v=... -o out.json",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("urls", nargs="*", metavar="URL",
                        help="one or more video URLs (any yt-dlp supported site)")
    parser.add_argument("--batch-file", metavar="PATH",
                        help="read URLs from a file, one per line ('-' for stdin)")
    parser.add_argument("--playlist", action="store_true",
                        help="expand playlist/channel URLs into their videos")
    parser.add_argument("--playlist-items", metavar="SPEC",
                        help="yt-dlp playlist selection, e.g. '1-10' or '1,3,5'")
    parser.add_argument("--max-entries", type=int, metavar="N",
                        help="cap how many videos a playlist expands to")

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output", metavar="PATH",
                        help="write to a file instead of stdout")
    output.add_argument("--jsonl", action="store_true",
                        help="emit one JSON object per line, streamed as each URL finishes")
    output.add_argument("--indent", type=int, default=2,
                        help="indentation for pretty JSON output")
    output.add_argument("-q", "--quiet", action="store_true",
                        help="suppress progress messages on stderr")
    output.add_argument("-v", "--verbose", action="store_true",
                        help="report every retry on stderr")

    subs = parser.add_argument_group("transcripts")
    subs.add_argument("--lang", default="en", metavar="CODES",
                      help="preferred caption languages, comma separated (en matches en-US)")
    subs.add_argument("--any-lang", action="store_true",
                      help="fall back to any available caption language")
    subs.add_argument("--no-subs", action="store_true",
                      help="skip transcripts entirely (metadata only, much faster)")
    subs.add_argument("--no-auto-subs", action="store_true",
                      help="ignore machine-generated captions")

    limits = parser.add_argument_group("rate limiting")
    limits.add_argument("--retries", type=int, default=4, metavar="N",
                        help="retries for rate_limit and transient failures")
    limits.add_argument("--blocked-retries", type=int, default=1, metavar="N",
                        help="retries for bot checks / login walls (deliberately small)")
    limits.add_argument("--base-backoff", type=float, default=2.0, metavar="SECONDS",
                        help="first backoff delay; doubles each attempt")
    limits.add_argument("--max-backoff", type=float, default=300.0, metavar="SECONDS",
                        help="upper bound on any single backoff wait")
    limits.add_argument("--sleep-interval", type=float, default=0.0, metavar="SECONDS",
                        help="pause between URLs to pace requests")
    limits.add_argument("--socket-timeout", type=float, default=30.0, metavar="SECONDS",
                        help="network timeout passed to yt-dlp")

    access = parser.add_argument_group("access")
    access.add_argument("--cookies", metavar="PATH",
                        help="Netscape-format cookies file passed to yt-dlp")
    access.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="BROWSER[+KEYRING][:PROFILE][::CONTAINER], e.g. firefox")
    access.add_argument("--proxy", metavar="URL", help="HTTP/SOCKS proxy URL")

    incidents = parser.add_argument_group("incident log")
    incidents.add_argument("--incident-log", default=DEFAULT_INCIDENT_LOG, metavar="PATH",
                           help="append-only JSONL log of rate_limit/blocked/transient events")
    incidents.add_argument("--no-incident-log", action="store_true",
                           help="do not write an incident log")

    parser.add_argument("--version", action="version",
                        version="%s %s" % (TOOL_NAME, TOOL_VERSION))
    return parser


def config_from_args(args):
    languages = tuple(code.strip() for code in args.lang.split(",") if code.strip())
    return Config(
        languages=languages or DEFAULT_LANGUAGES,
        any_language=args.any_lang,
        subtitles=not args.no_subs,
        auto_subtitles=not args.no_auto_subs,
        playlist=args.playlist,
        playlist_items=args.playlist_items,
        max_entries=args.max_entries,
        retries=args.retries,
        blocked_retries=args.blocked_retries,
        base_backoff=args.base_backoff,
        max_backoff=args.max_backoff,
        sleep_interval=args.sleep_interval,
        socket_timeout=args.socket_timeout,
        cookies=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        proxy=args.proxy,
        incident_log=args.incident_log,
        write_incidents=not args.no_incident_log,
        verbose=args.verbose,
    )


def main(argv=None, ydl_factory=default_ydl_factory):
    parser = build_parser()
    args = parser.parse_args(argv)

    urls = list(args.urls)
    if args.batch_file:
        try:
            urls.extend(read_batch_file(args.batch_file))
        except OSError as exc:
            parser.error("cannot read --batch-file: %s" % exc)
    if not urls:
        parser.error("no URLs given (pass URLs or --batch-file)")

    try:
        config = config_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    incidents = IncidentLogger(config.incident_log, enabled=config.write_incidents)
    handle = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    log = (lambda message: None) if args.quiet else (
        lambda message: sys.stderr.write(message + "\n"))

    records = []
    failures = 0
    try:
        with Extractor(config, incidents, ydl_factory) as extractor:
            targets = urls
            if config.playlist:
                targets = []
                for url in urls:
                    log("expanding %s" % url)
                    try:
                        expanded = extractor.expand(url)
                    except ExtractionError as exc:
                        failures += 1
                        record = build_error_record(url, exc)
                        records.append(record)
                        _emit(handle, record, args)
                        log("  failed (%s): %s" % (exc.category, exc.message))
                        continue
                    log("  %d video(s)" % len(expanded))
                    targets.extend(expanded)

            total = len(targets)
            for index, url in enumerate(targets):
                if index and config.sleep_interval:
                    time.sleep(config.sleep_interval)
                log("[%d/%d] %s" % (index + 1, total, url))
                try:
                    record = extractor.extract(url)
                except ExtractionError as exc:
                    failures += 1
                    record = build_error_record(url, exc)
                    log("  failed (%s): %s" % (exc.category, exc.message))
                else:
                    log("  ok: %s" % (record.get("title") or "(untitled)"))
                records.append(record)
                _emit(handle, record, args)

        if not args.jsonl:
            payload = records[0] if len(records) == 1 else records
            json.dump(payload, handle, indent=args.indent, ensure_ascii=False)
            handle.write("\n")
    except KeyboardInterrupt:  # pragma: no cover - interactive
        sys.stderr.write("interrupted\n")
        return 130
    finally:
        if handle is not sys.stdout:
            handle.close()

    if failures:
        where = ("; see %s" % config.incident_log) if config.write_incidents else ""
        log("%d of %d failed%s" % (failures, len(records), where))
    return 1 if failures else 0


def _emit(handle, record, args):
    """Stream a record immediately in JSONL mode; buffer otherwise."""
    if not args.jsonl:
        return
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    handle.flush()


if __name__ == "__main__":
    sys.exit(main())
