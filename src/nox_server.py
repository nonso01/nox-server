"""
Direct Python replica of lib.rs (the `nox_server` module).

All essential functions, constants, structs/classes, and helpers,
mirroring the original Rust module 1:1 in structure and behavior.
"""

from __future__ import annotations

import os
import re
import smtplib
import socket
import threading
import time
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------
class RateLimiter:
    """
    Replica of Rust's RateLimiter.

    Rust:   requests: Arc<Mutex<HashMap<String, Vec<Instant>>>>
    Python: a plain dict guarded by a threading.Lock — Python dicts aren't
            thread-safe for compound read-modify-write sequences the way
            this class performs them, so the lock is mandatory here (in
            Rust the Mutex was mandatory for the same reason: shared
            mutable state across threads).

    Rust's `Instant` (a monotonic clock) is replicated with
    `time.monotonic()`, NOT `time.time()`, since monotonic time is immune
    to system clock adjustments — exactly why Rust's std lib uses Instant
    over SystemTime for interval measurement.
    """

    def __init__(self, max_requests: int, window_minutes: int) -> None:
        self.requests: dict[str, list[float]] = {}
        self.max_requests: int = max_requests
        self.window_duration: float = window_minutes * 60  # seconds
        self._lock = threading.Lock()

    def is_allowed(self, client_id: str) -> bool:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window_duration

            # Rust: requests.entry(client_id).or_insert_with(Vec::new).retain(...)
            timestamps = self.requests.setdefault(client_id, [])
            timestamps[:] = [t for t in timestamps if t > cutoff]

            if len(timestamps) >= self.max_requests:
                return False
            else:
                timestamps.append(now)
                return True


# ---------------------------------------------------------------------------
# HttpResponse
# ---------------------------------------------------------------------------
class HttpResponse:
    """
    Replica of Rust's HttpResponse builder struct.

    Rust uses a consuming builder pattern (`mut self -> Self`), each method
    takes ownership and returns Self. Python has no ownership system, so
    these builder methods mutate `self` in place and return `self` —
    behaviorally identical for this use case (no aliasing issue arises
    because Python callers chain calls the same way Rust callers do).
    """

    def __init__(self, status: str) -> None:
        self.status: str = status
        self.content_type: str = "text/plain"
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
        """
        Rust: fn send(self, stream: &mut TcpStream, cors_origin_header: &str)
                  -> Result<(), std::io::Error>

        Python: raises OSError (the standard socket exception) on failure
        instead of returning a Result — the natural Python idiom. Callers
        that need Rust's explicit Result-handling style should wrap calls
        in try/except OSError, mirroring `.map_err(...)` call sites in
        main.rs.
        """
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
        # Rust's stream.flush() has no direct socket equivalent in Python
        # (TCP sockets without internal buffering flush via sendall itself);
        # omitted as a no-op equivalent.


# ---------------------------------------------------------------------------
# Email sending (lettre -> smtplib/email)
# ---------------------------------------------------------------------------
def send_email(to: str, subject: str, body: str) -> None:
    """
    Replica of send_email (plain text).

    Rust returns Result<(), Box<dyn Error>>; Python raises an exception
    (RuntimeError / smtplib exceptions) on failure instead. Callers
    (see send_confirmation_email in main.py) catch exceptions where Rust
    matched on Err(e).
    """
    smtp_user = os.environ.get("MY_EMAIL")
    if smtp_user is None:
        print("Warning: MY_EMAIL env was not set")
        raise RuntimeError("MY_EMAIL environment variable not set")

    smtp_pass = os.environ.get("MY_PASSWORD")
    if smtp_pass is None:
        print("Warning: MY_PASSWORD env was not set")
        raise RuntimeError("MY_PASSWORD environment variable not set")

    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    from_addr = os.environ.get("SMTP_FROM", smtp_user)

    msg = MIMEText(body, "plain")
    msg["From"] = from_addr
    msg["Reply-To"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject

    try:
        # lettre's SmtpTransport::relay implies an implicit TLS connection
        # (STARTTLS / SMTPS depending on port); SMTP_SSL on 465 mirrors
        # that "relay" behavior most closely for Gmail-style providers.
        with smtplib.SMTP_SSL(smtp_server, 465) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to], msg.as_string())
    except Exception as e:
        print(f"Failed to send email to: {to}")
        raise e

    print(f"Email sent successfully to: {to}")


def send_html_email(
    to: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
) -> None:
    """Replica of send_html_email — sends multipart/alternative when text_body is given."""
    smtp_user = os.environ.get("MY_EMAIL")
    if smtp_user is None:
        print("Warning: MY_EMAIL env was not set")
        raise RuntimeError("MY_EMAIL environment variable not set")

    smtp_pass = os.environ.get("MY_PASSWORD")
    if smtp_pass is None:
        print("Warning: MY_PASSWORD env was not set")
        raise RuntimeError("MY_PASSWORD environment variable not set")

    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    from_addr = os.environ.get("SMTP_FROM", smtp_user)

    if text_body is not None:
        # Rust: MultiPart::alternative().singlepart(text).singlepart(html)
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
    else:
        msg = MIMEText(html_body, "html")

    msg["From"] = from_addr
    msg["Reply-To"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject

    try:
        with smtplib.SMTP_SSL(smtp_server, 465) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to], msg.as_string())
    except Exception as e:
        print(f"Failed to send HTML email to: {to}")
        raise e

    print(f"HTML email sent successfully to: {to}")


def generate_email_html(name: str, message: str) -> str:
    """Helper function to generate HTML email template (verbatim port of the Rust format! template)."""
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Form Submission Received</title>
    <style>
        body {{
            font-family: system-ui, sans-serif, 'Ariel';
            line-height: 1.4;
            color: #222;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f4f4f4;
        }}
        .container {{
            width: max(350px, 70%);
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 10px;
            border: 2px solid #2222221c;
        }}
        .header {{
            text-align: center;
            color: #2c3e50;
            border-bottom: 2px solid #2222221c;
            padding-bottom: 20px;
            margin-bottom: 30px;
        }}
        .content {{
            font-size: 15px;
            margin-bottom: 30px;
        }}
        .highlight {{
            color: #34db69;
            font-weight: bold;
        }}
        .footer {{
            text-align: center;
            text-wrap: balance;
            color: #7f8c8d;
            border-top: 1px solid #ecf0f1;
            padding-top: 20px;
            margin-top: 30px;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤩 Thank You for Your Submission!</h1>
        </div>

        <div class="content">
            <p>Hello <span class="highlight">{name}</span>,</p>

            <p>Thank you for reaching out! I have received your message and will respond to your queries shortly 🫡.</p>

            <p><strong>Your message:</strong></p>
            <blockquote style="padding: 10px; border-left: 2px solid currentColor; margin: 10px 0; font-style: italic; font-size: 12px;">
                {message}
            </blockquote>

            <p>I appreciate you taking the time to contact me, and I'll get back to you as soon as possible ⚡️⚡️⚡️.</p>

            <p>Best regards,<br>
            <strong>Nonso Martin</strong></p>
        </div>

        <div class="footer">
            <p>This is an automated response to confirm we received your form submission.</p>
            <p>If you didn't initiate this message please ignore.</p>
        </div>
    </div>
</body>
</html>
    """


# ---------------------------------------------------------------------------
# Colored text
# ---------------------------------------------------------------------------
def green(text: str, bold: bool) -> str:
    if bold:
        return f"\x1b[2;32m{text}\x1b[0m"
    else:
        return f"\x1b[32m{text}\x1b[0m"


def red(text: str, bold: bool) -> str:
    if bold:
        return f"\x1b[2;31m{text}\x1b[0m"
    else:
        return f"\x1b[31m{text}\x1b[0m"


# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def html_escape(input_str: str) -> str:
    """HTML escaping function to prevent XSS. Order matches Rust exactly (chained .replace calls)."""
    return (
        input_str.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
        .replace("/", "&#x2F;")
    )


def sanitize_path(path: str) -> str:
    path_no_lead_slash = path[1:] if path.startswith("/") else path
    decoded = url_decode(path_no_lead_slash)
    parts = [
        part
        for part in decoded.split("/")
        if part != "" and part != "." and part != ".."
    ]
    return "/".join(parts)


def is_safe_path(path: Path) -> bool:
    """
    Replica of Rust's is_safe_path using Path.canonicalize().

    Rust's `canonicalize()` errors (Result::Err) if the path doesn't
    exist; Python's `Path.resolve(strict=True)` raises OSError/
    FileNotFoundError in the same circumstance, so it's used to mirror
    that fallibility instead of the non-strict `resolve()` (which
    silently succeeds on nonexistent paths and would NOT match Rust's
    Ok/Err branching below).
    """

    def _dist_path() -> Optional[Path]:
        try:
            current = Path.cwd()
        except OSError:
            return None
        parent = current.parent if current.parent != current else current
        return parent / "dist"

    try:
        canonical = path.resolve(strict=True)
        dist_path = _dist_path()
        if dist_path is None:
            return False
        try:
            canonical_dist = dist_path.resolve(strict=True)
            return _starts_with(canonical, canonical_dist)
        except OSError:
            return False
    except OSError:
        parent = path.parent
        try:
            canonical_parent = parent.resolve(strict=True)
            dist_path = _dist_path()
            if dist_path is None:
                return False
            try:
                canonical_dist = dist_path.resolve(strict=True)
                return _starts_with(canonical_parent, canonical_dist)
            except OSError:
                return False
        except OSError:
            return False


def _starts_with(path: Path, prefix: Path) -> bool:
    """Replica of Rust's PathBuf::starts_with (component-wise prefix check)."""
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
    return mime_map.get(ext, "application/octet-stream") if ext else "application/octet-stream"


def parse_multipart_data(body: str) -> dict[str, str]:
    form_data: dict[str, str] = {}
    parts = body.split("--")

    for part in parts:
        if "Content-Disposition: form-data" in part:
            name_marker = 'name="'
            name_start_idx = part.find(name_marker)
            if name_start_idx != -1:
                name_start = name_start_idx + len(name_marker)
                name_end = part[name_start:].find('"')
                if name_end != -1:
                    field_name = part[name_start:name_start + name_end]
                    value_start_idx = part.find("\r\n\r\n")
                    if value_start_idx != -1:
                        value_start = value_start_idx + 4
                        field_value = part[value_start:].strip()
                        if field_value != "":
                            form_data[field_name] = field_value
    return form_data


def parse_form_data(body: str) -> dict[str, str]:
    form_data: dict[str, str] = {}
    for pair in body.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)  # Rust's split_once -> split with maxsplit=1
            decoded_key = url_decode(key)
            decoded_value = url_decode(value)
            form_data[decoded_key] = decoded_value
    return form_data


def url_decode(s: str) -> str:
    result_chars: list[str] = []
    chars = list(s)
    i = 0
    n = len(chars)

    while i < n:
        ch = chars[i]
        if ch == "%":
            hex_str = "".join(chars[i + 1:i + 3])
            if len(hex_str) == 2:
                try:
                    byte = int(hex_str, 16)
                    result_chars.append(chr(byte))
                except ValueError:
                    pass  # Rust: if let Ok(byte) = ... else silently drop
            i += 3
        elif ch == "+":
            result_chars.append(" ")
            i += 1
        else:
            result_chars.append(ch)
            i += 1

    return "".join(result_chars)


def sanitize_email_content(input_str: str) -> str:
    """Sanitize email content to prevent header injection."""
    return "".join(c for c in input_str if c not in ("\r", "\n", "\0"))


def contains_potential_xss(input_str: str) -> bool:
    """Helper function to detect potential XSS attempts."""
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
