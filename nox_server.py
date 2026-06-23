"""
nox_server.py — core utilities for the nox HTTP server.
Rate limiter, HTTP response builder, Telegram notification,
path helpers, MIME types, form parsers, and XSS detection.
"""

from __future__ import annotations

# import os
import socket
import threading
import time
from pathlib import Path
# from typing import Optional


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------
class RateLimiter:
    """
    Per-IP sliding-window rate limiter, shared across all worker threads.

    Uses a plain dict guarded by a threading.Lock — the lock is mandatory
    because the read-then-modify sequence on the timestamps list is not
    atomic, and multiple workers access this concurrently.

    time.monotonic() is used instead of time.time() because monotonic
    time is immune to system clock adjustments (DST, NTP corrections, etc),
    which matter for interval measurement.
    """

    def __init__(self, max_requests: int, window_minutes: int) -> None:
        self.requests: dict[str, list[float]] = {}
        self.max_requests = max_requests
        self.window_duration = window_minutes * 60  # convert to seconds
        self._lock = threading.Lock()

    def is_allowed(self, client_id: str) -> bool:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window_duration
            timestamps = self.requests.setdefault(client_id, [])
            # Drop timestamps outside the current window
            timestamps[:] = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self.max_requests:
                return False
            timestamps.append(now)
            return True


# ---------------------------------------------------------------------------
# HttpResponse
# ---------------------------------------------------------------------------
class HttpResponse:
    """
    Fluent HTTP response builder.

    Builder methods mutate and return self so calls can be chained:
        HttpResponse.ok().json('{"a":1}').send(stream, cors_header)
    """

    def __init__(self, status: str) -> None:
        self.status = status
        self.content_type = "text/plain"
        self.body: bytes = b""
        self.custom_headers: list[str] = []

    @staticmethod
    def new(status: str) -> "HttpResponse":
        return HttpResponse(status)

    @staticmethod
    def ok() -> "HttpResponse":
        return HttpResponse.new("200 OK")

    @staticmethod
    def not_found() -> "HttpResponse":
        return HttpResponse.new("404 Not Found").html("<h1>404 Not Found</h1>")

    @staticmethod
    def bad_request() -> "HttpResponse":
        return HttpResponse.new("400 Bad Request")

    @staticmethod
    def too_many_requests() -> "HttpResponse":
        return HttpResponse.new("429 Too Many Requests")

    @staticmethod
    def method_not_allowed() -> "HttpResponse":
        return HttpResponse.new("405 Method Not Allowed")

    def json(self, body: str) -> "HttpResponse":
        self.content_type = "application/json"
        self.body = body.encode("utf-8")
        return self

    def html(self, body: str) -> "HttpResponse":
        self.content_type = "text/html; charset=utf-8"
        self.body = body.encode("utf-8")
        return self

    def text(self, body: str) -> "HttpResponse":
        self.content_type = "text/plain"
        self.body = body.encode("utf-8")
        return self

    def with_header(self, header: str) -> "HttpResponse":
        self.custom_headers.append(header)
        return self

    def send(self, stream: socket.socket, cors_origin_header: str) -> None:
        """Write the full HTTP response to the socket. Raises OSError on failure."""
        cors_headers = f"{cors_origin_header}\r\n" if cors_origin_header else ""
        custom_headers = (
            "\r\n".join(self.custom_headers) + "\r\n" if self.custom_headers else ""
        )
        response = (
            f"HTTP/1.1 {self.status}\r\n"
            f"Content-Type: {self.content_type}\r\n"
            f"Content-Length: {len(self.body)}\r\n"
            f"{cors_headers}{custom_headers}\r\n"
        )
        stream.sendall(response.encode("utf-8"))
        stream.sendall(self.body)


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------
def send_telegram_notification(form_data: dict[str, str]) -> None:
    """
    Sends a contact form submission to Telegram.
    Delegates to TelegramBot in telegram_bot.py — all formatting
    and API logic lives there, keeping this module free of duplication.
    """
    from telegram_bot import TelegramBot

    TelegramBot().send_form_submission(form_data)


# ---------------------------------------------------------------------------
# Colored terminal output
# ---------------------------------------------------------------------------
def green(text: str, bold: bool = False) -> str:
    return f"\x1b[2;32m{text}\x1b[0m" if bold else f"\x1b[32m{text}\x1b[0m"


def red(text: str, bold: bool = False) -> str:
    return f"\x1b[2;31m{text}\x1b[0m" if bold else f"\x1b[31m{text}\x1b[0m"


# ---------------------------------------------------------------------------
# Path and file helpers
# ---------------------------------------------------------------------------
def html_escape(input_str: str) -> str:
    """Escapes HTML special characters to prevent XSS in rendered output."""
    return (
        input_str.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
        .replace("/", "&#x2F;")
    )


def sanitize_path(path: str) -> str:
    """
    Strips the leading slash, URL-decodes, and removes any path traversal
    segments (. and ..) before the path is joined onto a base directory.
    """
    path_no_lead_slash = path[1:] if path.startswith("/") else path
    decoded = url_decode(path_no_lead_slash)
    parts = [p for p in decoded.split("/") if p and p != "." and p != ".."]
    return "/".join(parts)


def is_safe_path(path: Path, base_dir: Path) -> bool:
    """
    Confirms that `path` resolves to somewhere inside `base_dir`,
    preventing directory traversal even after sanitize_path runs.

    Checks the parent directory when the file itself doesn't exist yet
    (e.g. a request for a file that returns 404 — we still want to
    confirm the attempted path wasn't escaping the base).
    """
    try:
        canonical = path.resolve(strict=True)
        canonical_base = base_dir.resolve(strict=True)
        return _starts_with(canonical, canonical_base)
    except OSError:
        try:
            canonical_parent = path.parent.resolve(strict=True)
            canonical_base = base_dir.resolve(strict=True)
            return _starts_with(canonical_parent, canonical_base)
        except OSError:
            return False


def _starts_with(path: Path, prefix: Path) -> bool:
    """Component-wise prefix check — avoids false positives from string prefix matching."""
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


def get_mime_type(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower() if path.suffix else None
    mime_map = {
        "html": "text/html",
        "htm": "text/html",
        "css": "text/css",
        "js": "application/javascript",
        "json": "application/json",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "ico": "image/x-icon",
        "woff": "font/woff",
        "woff2": "font/woff2",
        "ttf": "font/ttf",
        "eot": "application/vnd.ms-fontobject",
        "glb": "model/gltf-binary",
        "gltf": "model/gltf+json",
        "mp4": "video/mp4",
        "webm": "video/webm",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "pdf": "application/pdf",
        "zip": "application/zip",
        "txt": "text/plain",
    }
    return (
        mime_map.get(ext, "application/octet-stream")
        if ext
        else "application/octet-stream"
    )


# ---------------------------------------------------------------------------
# Form body parsers
# ---------------------------------------------------------------------------
def parse_multipart_data(body: str) -> dict[str, str]:
    form_data: dict[str, str] = {}
    for part in body.split("--"):
        if "Content-Disposition: form-data" not in part:
            continue
        name_marker = 'name="'
        name_start_idx = part.find(name_marker)
        if name_start_idx == -1:
            continue
        name_start = name_start_idx + len(name_marker)
        name_end = part[name_start:].find('"')
        if name_end == -1:
            continue
        field_name = part[name_start : name_start + name_end]
        value_start_idx = part.find("\r\n\r\n")
        if value_start_idx == -1:
            continue
        field_value = part[value_start_idx + 4 :].strip()
        if field_value:
            form_data[field_name] = field_value
    return form_data


def parse_form_data(body: str) -> dict[str, str]:
    form_data: dict[str, str] = {}
    for pair in body.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            form_data[url_decode(key)] = url_decode(value)
    return form_data


def url_decode(s: str) -> str:
    """Decodes percent-encoded characters and converts '+' to spaces."""
    result: list[str] = []
    chars = list(s)
    i = 0
    while i < len(chars):
        ch = chars[i]
        if ch == "%" and i + 2 < len(chars):
            hex_str = "".join(chars[i + 1 : i + 3])
            try:
                result.append(chr(int(hex_str, 16)))
            except ValueError:
                pass  # malformed percent sequence — silently skip
            i += 3
        elif ch == "+":
            result.append(" ")
            i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------
def contains_potential_xss(input_str: str) -> bool:
    """
    Detects common XSS attack patterns before any content reaches a
    response. Covers script tags, event handlers, data URIs, and
    CSS-based injection vectors.
    """
    input_lower = input_str.lower()
    xss_patterns = [
        "<script",
        "</script",
        "javascript:",
        "vbscript:",
        "onload=",
        "onerror=",
        "onclick=",
        "onmouseover=",
        "onfocus=",
        "onblur=",
        "onchange=",
        "onsubmit=",
        "<iframe",
        "<object",
        "<embed",
        "<applet",
        "<meta",
        "<link",
        "data:text/html",
        "data:application",
        "&#",
        "&#x",
        "\\u",
        "\\x",
        "expression(",
        "url(",
        "@import",
        "behavior:",
        "-moz-binding:",
    ]
    return any(pattern in input_lower for pattern in xss_patterns)
