"""
main.py — nox HTTP server entry point.

Thread pool, routing, request/header parsing, form validation,
static file serving, and response generation.
"""

from __future__ import annotations

import os
import queue
import re
import socket
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import constants as const
import nox_server as nox
from nox_server import HttpResponse, RateLimiter


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------
def load_dotenv(path: str = ".env") -> None:
    """
    Minimal stdlib .env loader — no external packages required.

    Rules:
      - Blank lines and lines starting with '#' are skipped.
      - KEY=VALUE pairs are split on the first '='.
      - Surrounding quotes (single or double) are stripped from values.
      - Variables already set in the real environment are never overwritten
        (shell exports always take priority over the .env file).
      - Missing file is a silent no-op.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Security configuration
# ---------------------------------------------------------------------------
@dataclass
class SecurityConfig:
    max_content_length: int
    max_connections: int
    connection_timeout: float
    max_requests_per_hour: int
    enable_rate_limiting: bool

    @staticmethod
    def new() -> "SecurityConfig":
        return SecurityConfig(
            max_content_length=const.MAX_CONTENT_LENGTH,
            max_connections=100,
            connection_timeout=30.0,
            max_requests_per_hour=100,
            enable_rate_limiting=True,
        )


# ---------------------------------------------------------------------------
# Route enum
# ---------------------------------------------------------------------------
class Route(Enum):
    STATIC = auto()
    CONTACT_FORM = auto()
    API_STATUS = auto()
    API_HEALTH = auto()
    NOT_FOUND = auto()


def match_route(method: str, path: str) -> Route:
    match (method, path):
        case ("POST", "/contact") | ("POST", "/api/contact"):
            return Route.CONTACT_FORM
        case ("GET", "/api/status"):
            return Route.API_STATUS
        case ("GET", "/api/health"):
            return Route.API_HEALTH
        case ("GET", not_api_path) if not_api_path.startswith("/api/"):
            return Route.NOT_FOUND
        case ("GET", _):
            return Route.STATIC
        case _:
            return Route.NOT_FOUND


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------
class ServerError(Exception):
    pass


class IoError(ServerError):
    def __init__(self, e: Exception) -> None:
        super().__init__(f"IO error: {e}")


class ParseError(ServerError):
    def __init__(self, msg: str) -> None:
        super().__init__(f"Parse error: {msg}")


class ValidationError(ServerError):
    def __init__(self, msg: str) -> None:
        super().__init__(f"Validation error: {msg}")


class ThreadPoolError(ServerError):
    def __init__(self, msg: str) -> None:
        super().__init__(f"Thread pool error: {msg}")


class NetworkError(ServerError):
    def __init__(self, msg: str) -> None:
        super().__init__(f"Network error: {msg}")


# ---------------------------------------------------------------------------
# CORS configuration
# ---------------------------------------------------------------------------
@dataclass
class CorsConfig:
    allow_origins: list[str]
    allow_all_origins: bool
    allow_methods: list[str]
    allow_headers: list[str]
    max_age: int

    @staticmethod
    def new() -> "CorsConfig":
        cors_mode = os.environ.get("CORS_MODE", "same-origin")
        if cors_mode == "cross-origin":
            return CorsConfig.cross_origin_config()
        return CorsConfig.same_origin_config()

    @staticmethod
    def cross_origin_config() -> "CorsConfig":
        return CorsConfig(
            allow_origins=[
                "http://localhost:5173",
                "https://nox-dev.vercel.app",
            ],
            allow_all_origins=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Authorization",
                "X-Requested-With",
                "Accept",
                "Origin",
                "ngrok-skip-browser-warning",
            ],
            max_age=const.CORS_CONFIG_MAX_AGE,
        )

    @staticmethod
    def same_origin_config() -> "CorsConfig":
        return CorsConfig(
            allow_origins=[],
            allow_all_origins=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Accept"],
            max_age=const.CORS_CONFIG_MIN_AGE,
        )

    def is_method_allowed(self, method: str) -> bool:
        return method.upper() in self.allow_methods

    def is_origin_allowed(self, origin: str) -> bool:
        if self.allow_all_origins:
            return True
        if not self.allow_origins:
            return True  # same-origin mode — no explicit list needed
        return origin in self.allow_origins


# ---------------------------------------------------------------------------
# Thread pool
# ---------------------------------------------------------------------------
class Worker:
    """
    One OS thread pulling connection sockets from the shared task queue.

    Receives None as a shutdown sentinel — the main thread pushes one
    sentinel per worker during shutdown, causing each worker's loop to exit.
    The socket is explicitly closed in the finally block because Python
    sockets have no automatic close on scope exit (unlike Rust's Drop).
    """

    def __init__(
        self,
        worker_id: int,
        task_queue: "queue.Queue[Optional[socket.socket]]",
        cors_config: CorsConfig,
        rate_limiter: RateLimiter,
        security_config: SecurityConfig,
    ) -> None:
        self.id = worker_id
        self.thread = threading.Thread(
            target=self._run,
            args=(task_queue, cors_config, rate_limiter, security_config),
            daemon=False,
        )
        self.thread.start()

    def _run(
        self,
        task_queue: "queue.Queue[Optional[socket.socket]]",
        cors_config: CorsConfig,
        rate_limiter: RateLimiter,
        security_config: SecurityConfig,
    ) -> None:
        while True:
            stream = task_queue.get()
            if stream is None:
                print(f"Worker {self.id} shutting down")
                break
            try:
                handle_connection_safe(
                    stream, cors_config, rate_limiter, security_config
                )
            except Exception as e:
                print(f"Worker {self.id} error: {e}")
            finally:
                # Explicitly close the socket so the browser receives EOF
                # and doesn't treat it as keep-alive, which would cause the
                # next request on that socket to hang indefinitely.
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()


class ThreadPool:
    def __init__(
        self,
        size: int,
        cors_config: CorsConfig,
        security_config: SecurityConfig,
    ) -> None:
        if size == 0:
            raise ThreadPoolError("Thread pool size cannot be zero")

        self._queue: "queue.Queue[Optional[socket.socket]]" = queue.Queue()
        self.security_config = security_config

        # One shared RateLimiter across all workers
        self.rate_limiter = RateLimiter(
            security_config.max_requests_per_hour,
            const.WINDOW_LIMIT_MINS,
        )

        self.workers: list[Worker] = []
        for worker_id in range(size):
            try:
                self.workers.append(
                    Worker(
                        worker_id,
                        self._queue,
                        cors_config,
                        self.rate_limiter,
                        security_config,
                    )
                )
            except Exception as e:
                raise ThreadPoolError(f"Failed to create worker {worker_id}: {e}")

        self._closed = False

    def execute(self, stream: socket.socket) -> None:
        try:
            stream.settimeout(self.security_config.connection_timeout)
        except OSError as e:
            print(f"Warning: Could not set socket timeout: {e}")
        try:
            self._queue.put(stream)
        except Exception as e:
            raise ThreadPoolError(f"Failed to enqueue task: {e}")

    def shutdown(self) -> None:
        """Push one sentinel per worker, then wait for all threads to exit."""
        if self._closed:
            return
        self._closed = True
        for _ in self.workers:
            self._queue.put(None)
        for worker in self.workers:
            print(f"Shutting down worker {worker.id}")
            worker.thread.join()


# ---------------------------------------------------------------------------
# BufReader — buffered line/byte reader over a raw socket
# ---------------------------------------------------------------------------
class BufReader:
    """
    Python sockets have no built-in buffered reader. This class provides
    read_line() (reads up to and including '\n') and read_exact(n) (reads
    exactly n bytes), both of which are needed to parse raw HTTP requests.
    """

    def __init__(self, stream: socket.socket, bufsize: int = 4096) -> None:
        self._stream = stream
        self._buf = b""
        self._bufsize = bufsize

    def get_mut(self) -> socket.socket:
        return self._stream

    def read_line(self) -> str:
        """Returns '' on immediate EOF (connection closed before any data)."""
        while b"\n" not in self._buf:
            try:
                chunk = self._stream.recv(self._bufsize)
            except OSError:
                chunk = b""
            if not chunk:
                break
            self._buf += chunk

        if b"\n" in self._buf:
            idx = self._buf.index(b"\n") + 1
            line, self._buf = self._buf[:idx], self._buf[idx:]
        else:
            line, self._buf = self._buf, b""

        return line.decode("utf-8", errors="replace")

    def read_exact(self, n: int) -> bytes:
        """Raises IoError if the connection closes before n bytes are received."""
        while len(self._buf) < n:
            try:
                chunk = self._stream.recv(self._bufsize)
            except OSError as e:
                raise IoError(e)
            if not chunk:
                raise IoError(OSError("connection closed before all bytes received"))
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------
def parse_headers_secure(
    buf_reader: BufReader,
    security_config: SecurityConfig,
) -> tuple[int, Optional[str], Optional[str]]:
    """
    Reads and parses request headers with security limits.

    Returns (content_length, origin, forwarded_for).

    X-Forwarded-For is captured here so handle_connection_safe can use
    the real client IP (not the proxy's address) for rate limiting.
    We trust this header only because traffic arrives via Railway/ngrok,
    which overwrites it rather than passing through client-supplied values.
    If this server were ever exposed directly to the internet without a
    trusted proxy, X-Forwarded-For would need to be ignored or verified.
    """
    content_length = 0
    origin: Optional[str] = None
    forwarded_for: Optional[str] = None
    header_count = 0
    max_headers = 50

    while True:
        line = buf_reader.read_line()
        if line.strip() == "":
            break

        header_count += 1
        if header_count > max_headers:
            raise ParseError("Too many headers")
        if len(line) > const.MAX_HEADER_LINE_SIZE:
            raise ParseError("Header line too long")

        line_lower = line.lower()

        if line_lower.startswith("content-length:"):
            parts = line.split(": ", 1)
            if len(parts) > 1:
                try:
                    content_length = int(parts[1].strip())
                except ValueError:
                    raise ParseError("Invalid Content-Length")
                if content_length > security_config.max_content_length:
                    raise ValidationError(
                        f"Content-Length {content_length} exceeds limit "
                        f"{security_config.max_content_length}"
                    )

        elif line_lower.startswith("origin:"):
            parts = line.split(": ", 1)
            if len(parts) > 1:
                value = parts[1].strip()
                if len(value) > 200:
                    raise ParseError("Origin header too long")
                origin = value

        elif line_lower.startswith("x-forwarded-for:"):
            parts = line.split(": ", 1)
            if len(parts) > 1:
                value = parts[1].strip()
                if len(value) > 200:
                    raise ParseError("X-Forwarded-For header too long")
                # X-Forwarded-For can be a comma-separated chain when multiple
                # proxies are involved ("client, proxy1, proxy2"). The first
                # entry is always the original client IP.
                first_ip = value.split(",")[0].strip()
                if first_ip:
                    forwarded_for = first_ip

    return content_length, origin, forwarded_for


# ---------------------------------------------------------------------------
# CORS helpers
# ---------------------------------------------------------------------------
def get_cors_origin_header(cors_config: CorsConfig, origin: Optional[str]) -> str:
    if origin is None:
        return "Access-Control-Allow-Origin: *" if cors_config.allow_all_origins else ""
    if not cors_config.is_origin_allowed(origin):
        return ""
    if cors_config.allow_all_origins:
        return "Access-Control-Allow-Origin: *"
    return (
        f"Access-Control-Allow-Origin: {origin}\r\n"
        f"Access-Control-Allow-Credentials: true"
    )


def handle_preflight_request(
    stream: socket.socket,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    origin_header = get_cors_origin_header(cors_config, origin)

    if origin_header == "" and origin is not None:
        stream.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        print(nox.red(f"⛔️ Blocked preflight from unauthorized origin: {origin!r}"))
        return

    response = (
        f"HTTP/1.1 200 OK\r\n{origin_header}\r\n"
        f"Access-Control-Allow-Methods: {', '.join(cors_config.allow_methods)}\r\n"
        f"Access-Control-Allow-Headers: {', '.join(cors_config.allow_headers)}\r\n"
        f"Access-Control-Max-Age: {cors_config.max_age}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    stream.sendall(response.encode("utf-8"))
    print(f"Sent preflight response for origin: {origin!r}")


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------
DIST_DIR = Path("./dist")
PAGES_DIR = Path("./pages")


def serve_static_file_with_cors(
    stream: socket.socket,
    path: str,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    """
    Serves files from ./dist when present, falling back to ./pages otherwise.
    If neither exists, serves an inline confirmation page so a bare deployment
    still returns something useful rather than a connection error.

    ./dist  — production frontend build (Vite/React output)
    ./pages — fallback HTML pages (index.html, success.html, error.html)
    """
    if DIST_DIR.exists():
        _serve_from_dir(stream, path, DIST_DIR, cors_config, origin)
    elif PAGES_DIR.exists():
        _serve_from_dir(stream, path, PAGES_DIR, cors_config, origin)
    else:
        _serve_bare_fallback(stream, cors_config, origin)


def _serve_bare_fallback(
    stream: socket.socket,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    """
    Ultimate fallback — shown when neither dist/ nor pages/ exist.
    Confirms the server is running without crashing or returning a
    connection error to the browser.
    """
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Server Running</title>
    <style>
        body { font-family: system-ui, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
        .status { color: #34db69; }
        .warning { background: #fff3cd; padding: 10px; border-radius: 4px; margin-top: 1rem; }
        code { background: #eee; padding: 2px 5px; border-radius: 3px; }
    </style>
</head>
<body>
    <h1 class="status">&#x2714; Server is Running</h1>
    <div class="warning">
        Neither <code>dist/</code> nor <code>pages/</code> were found.<br>
        This is a fallback page confirming the server itself is alive.
    </div>
    <p style="margin-top:1rem">Endpoints available:</p>
    <ul>
        <li><code>GET /api/health</code></li>
        <li><code>GET /api/status</code></li>
        <li><code>POST /contact</code></li>
    </ul>
</body>
</html>"""
    send_file_response_with_cors(
        stream, html.encode("utf-8"), "text/html", cors_config, origin
    )


def _serve_from_dir(
    stream: socket.socket,
    path: str,
    base_dir: Path,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    safe_path = nox.sanitize_path(path)
    # Root path resolves to index.html
    file_path = base_dir / (safe_path if safe_path else "index.html")

    if not nox.is_safe_path(file_path, base_dir):
        send_error_response_with_cors(
            stream, "403 Forbidden", "Access denied", cors_config, origin
        )
        return

    try:
        content = file_path.read_bytes()
        send_file_response_with_cors(
            stream, content, nox.get_mime_type(file_path), cors_config, origin
        )
    except OSError:
        send_error_response_with_cors(
            stream, "404 Not Found", "File not found", cors_config, origin
        )


def send_file_response_with_cors(
    stream: socket.socket,
    content: bytes,
    mime_type: str,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    origin_header = get_cors_origin_header(cors_config, origin)
    cors_headers = f"{origin_header}\r\n" if origin_header else ""
    response = (
        f"HTTP/1.1 200 OK\r\nContent-Type: {mime_type}\r\n"
        f"Content-Length: {len(content)}\r\n{cors_headers}\r\n"
    )
    stream.sendall(response.encode("utf-8"))
    stream.sendall(content)


def send_error_response_with_cors(
    stream: socket.socket,
    status: str,
    message: str,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    origin_header = get_cors_origin_header(cors_config, origin)
    cors_headers = f"{origin_header}\r\n" if origin_header else ""
    html = (
        f"<!DOCTYPE html><html><head><title>{status}</title></head>"
        f"<body><h1>{status}</h1><p>{message}</p></body></html>"
    )
    response = (
        f"HTTP/1.1 {status}\r\nContent-Type: text/html\r\n"
        f"Content-Length: {len(html.encode('utf-8'))}\r\n{cors_headers}\r\n{html}"
    )
    stream.sendall(response.encode("utf-8"))


# ---------------------------------------------------------------------------
# Form validation
# ---------------------------------------------------------------------------
# Compiled once at module load — avoids recompiling per request.
# Pattern is deliberately simple to resist ReDoS (catastrophic backtracking).
_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def validate_form_data(form_data: dict[str, str]) -> None:
    """Raises ValidationError describing every problem found, not just the first."""
    allowed_fields = {c.name for c in const.FIELD_CONSTRAINTS}
    unexpected = [k for k in form_data if k not in allowed_fields]
    if unexpected:
        raise ValidationError(f"Unexpected fields: {unexpected!r}")

    errors: list[str] = []

    for constraint in const.FIELD_CONSTRAINTS:
        value = form_data.get(constraint.name)

        if value is None:
            if constraint.required:
                errors.append(f"Missing required field: {constraint.name}")
            continue

        # Null bytes and non-printable control characters (Unicode category Cc)
        # excluding the whitespace controls \n \r \t which are legitimately used
        # in message bodies.
        has_control_char = any(
            unicodedata.category(c) == "Cc" and c not in ("\n", "\r", "\t")
            for c in value
        )
        if has_control_char:
            errors.append(f"{constraint.name} contains invalid characters")
            continue

        if len(value) > constraint.max_length:
            errors.append(
                f"{constraint.name} too long "
                f"(max {constraint.max_length}, got {len(value)})"
            )

        if constraint.email:
            if len(value) > const.RFC_5321_MAX_EMAIL_LENGTH:
                errors.append("Email address too long")
            elif not _EMAIL_REGEX.match(value):
                errors.append("Invalid email format")
            else:
                local, domain = value.split("@", 1)
                if len(local) > const.RFC_5321_MAX_LOCAL_PART_LENGTH:
                    errors.append("Email local part too long")
                if len(domain) > const.RFC_5321_MAX_DOMAIN_PART_LENGTH:
                    errors.append("Email domain too long")

        if constraint.name == "message":
            suspicious_patterns = [
                "http://",
                "https://",
                "www.",
                ".com",
                ".org",
                ".net",
                "<script",
                "<iframe",
                "javascript:",
                "data:",
            ]
            count = sum(1 for p in suspicious_patterns if p in value.lower())
            if count > 2:
                errors.append("Message contains suspicious content")

    for cb_name in const.OPTIONAL_CHECKBOX:
        cb_value = form_data.get(cb_name)
        if cb_value is not None and cb_value != "on":
            errors.append(f"Invalid value for checkbox '{cb_name}'")

    if sum(len(v) for v in form_data.values()) > const.MAX_FORM_DATA_LENGTH:
        errors.append("Total form data too large")

    for field_name, value in form_data.items():
        if nox.contains_potential_xss(value):
            errors.append(
                f"Field '{field_name}' contains potentially malicious content"
            )

    if errors:
        raise ValidationError(", ".join(errors))


# ---------------------------------------------------------------------------
# POST /contact handler
# ---------------------------------------------------------------------------
def handle_post_request_secure(
    buf_reader: BufReader,
    content_length: int,
    cors_config: CorsConfig,
    origin: Optional[str],
    security_config: SecurityConfig,
    client_ip: str,
) -> None:
    if content_length == 0:
        _send_page_response(buf_reader, "error", cors_config, origin)
        return

    if content_length > security_config.max_content_length:
        print(f"⛔️ Payload too large from {client_ip}: {content_length // 1024}KB")
        _send_page_response(buf_reader, "error", cors_config, origin)
        return

    try:
        body = buf_reader.read_exact(content_length)
    except IoError as e:
        print(f"⛔️ Failed to read POST body from {client_ip}: {e}")
        raise

    body_str = body.decode("utf-8", errors="replace")
    print(f"📨 POST data received from {client_ip} ({len(body)}B)")

    form_data = (
        nox.parse_multipart_data(body_str)
        if "Content-Disposition: form-data" in body_str
        else nox.parse_form_data(body_str)
    )

    try:
        validate_form_data(form_data)
    except ValidationError as e:
        print(f"⛔️ Validation failed for {client_ip}: {e}")
        _send_page_response(buf_reader, "error", cors_config, origin)
        return

    print(f"✅ Form validation passed for {client_ip}")

    for key, value in form_data.items():
        if key == "email":
            print(f"📧 Field '{key}': '***@***'")
        elif key == "message":
            print(f"📝 Field '{key}': '{value[:50]}' ({len(value)}chars)")
        else:
            print(f"📄 Field '{key}': '{value}'")

    notify_via_telegram(form_data, client_ip)
    _send_page_response(buf_reader, "success", cors_config, origin)


def notify_via_telegram(form_data: dict[str, str], client_ip: str) -> bool:
    try:
        nox.send_telegram_notification(form_data)
        print(f"✅ Telegram notification sent for submission from {client_ip}")
        return True
    except Exception as e:
        print(f"⛔️ Telegram notification failed from {client_ip}: {e}")
        return False


# ---------------------------------------------------------------------------
# HTML page responses (loaded from ./pages/)
# ---------------------------------------------------------------------------
def _send_page_response(
    buf_reader: BufReader,
    page: str,  # "success" | "error"
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    """
    Loads success.html or error.html from ./pages/ and sends it as the
    POST response. Falls back to an inline string only if the file is missing.

    Moving HTML out of Python strings and into ./pages/ keeps the server
    code clean and lets you edit the response pages without touching main.py.
    """
    page_path = PAGES_DIR / f"{page}.html"
    try:
        html_content = page_path.read_text(encoding="utf-8")
    except OSError:
        # Ultimate fallback — fires when pages/ is missing or incomplete.
        # Keeps the server functional and gives a visible confirmation
        # that it's running, even in a bare deployment with no HTML files.
        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
     <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400..900&family=Outfit:wght@100..900&display=swap"
      rel="stylesheet"
    />
    <title>{"Success" if page == "success" else "Error"}</title>
    <style>
        * {{ padding: 0; margin: 0; box-sizing: border-box; }}
        body {{
            font-family: "Outfit", system-ui, sans-serif;
            background: #222; color: white;
            min-height: 100vh; display: grid; place-content: center;
        }}
        .container {{
            background: #101010; width: 500px; border-radius: 15px;
            padding: 2rem 3rem; display: flex; flex-direction: column; gap: 1.5rem;
        }}
        h1 {{ text-align: center; }}
        .message {{
            border-left: 4px solid {"#51cf66" if page == "success" else "#fa4646"};
            padding: 1rem; border-radius: 5px;
        }}
        a {{
            display: grid; place-content: center; width: 40%; padding: 0.5rem;
            background: #222; color: white; text-decoration: none;
            border-radius: 5px; border: 1px solid #474646;
        }}
        a:hover {{ background: #34db69; }}
        @media (max-width: 510px) {{ .container {{ width: 97dvw; }} a {{ width: 60%; }} }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{"🥂 Success!" if page == "success" else "⛔️ Error"}</h1>
        <div class="message">{"Form submitted successfully!" if page == "success" else "Something went wrong. Please try again."}</div>
        <a href="/">Back to Homepage</a>
    </div>
</body>
</html>"""

    origin_header = get_cors_origin_header(cors_config, origin)
    cors_headers = f"{origin_header}\r\n" if origin_header else ""
    encoded = html_content.encode("utf-8")
    response = (
        f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(encoded)}\r\n"
        f"X-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
        f"X-XSS-Protection: 1; mode=block\r\n{cors_headers}\r\n"
    )
    stream = buf_reader.get_mut()
    stream.sendall(response.encode("utf-8"))
    stream.sendall(encoded)


# ---------------------------------------------------------------------------
# API route handlers
# ---------------------------------------------------------------------------
def handle_api_status(
    stream: socket.socket,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    timestamp = int(time.time())
    body = f'{{"status":"healthy","version":"1.0.0","timestamp":{timestamp}}}'
    try:
        HttpResponse.ok().json(body).send(
            stream, get_cors_origin_header(cors_config, origin)
        )
    except OSError as e:
        raise IoError(e)


def handle_api_health(
    stream: socket.socket,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    try:
        HttpResponse.ok().text("OK").send(
            stream, get_cors_origin_header(cors_config, origin)
        )
    except OSError as e:
        raise IoError(e)


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------
def handle_connection_safe(
    stream: socket.socket,
    cors_config: CorsConfig,
    rate_limiter: RateLimiter,
    security_config: SecurityConfig,
) -> None:
    # Raw socket IP — accurate for direct connections, but when traffic
    # arrives via Railway/ngrok this is the proxy's address, not the
    # real visitor. X-Forwarded-For (parsed below) gives the real IP.
    try:
        socket_ip = stream.getpeername()[0]
    except OSError:
        socket_ip = "unknown"

    buf_reader = BufReader(stream)
    request_line = buf_reader.read_line()

    if not request_line or not request_line.strip():
        raise NetworkError("Empty or missing request line")

    if len(request_line) > const.MAX_REQUEST_LINE_SIZE:
        send_error_response_with_cors(
            stream,
            "414 Request-URI Too Long",
            "Request line too long",
            cors_config,
            None,
        )
        return

    content_length, origin, forwarded_for = parse_headers_secure(
        buf_reader, security_config
    )

    # Prefer the real visitor IP from X-Forwarded-For for rate limiting.
    # See parse_headers_secure docstring for the trust model.
    client_ip = forwarded_for if forwarded_for else socket_ip

    if security_config.enable_rate_limiting and not rate_limiter.is_allowed(client_ip):
        print(f"⛔️ Rate limit exceeded for IP: {client_ip}")
        send_error_response_with_cors(
            stream,
            "429 Too Many Requests",
            "Rate limit exceeded. Please try again later.",
            cors_config,
            None,
        )
        return

    parts = request_line.split()
    if len(parts) < 2:
        send_error_response_with_cors(
            stream, "400 Bad Request", "Invalid request line", cors_config, None
        )
        return

    method, path = parts[0], parts[1]
    print(
        f"📥 {method} {path} from {client_ip} (Content: {content_length // const.ONE_KILO_BYTE}KB)"
    )

    if not cors_config.is_method_allowed(method):
        print(f"🚫 Method {method} not allowed")
        send_error_response_with_cors(
            stream, "405 Method Not Allowed", "Method not allowed", cors_config, origin
        )
        return

    if method == "OPTIONS":
        handle_preflight_request(stream, cors_config, origin)
        return

    route = match_route(method, path)

    if route == Route.CONTACT_FORM:
        handle_post_request_secure(
            buf_reader, content_length, cors_config, origin, security_config, client_ip
        )
    elif route == Route.API_STATUS:
        handle_api_status(stream, cors_config, origin)
    elif route == Route.API_HEALTH:
        handle_api_health(stream, cors_config, origin)
    elif route == Route.STATIC:
        serve_static_file_with_cors(stream, path, cors_config, origin)
    else:
        try:
            HttpResponse.not_found().send(
                stream, get_cors_origin_header(cors_config, origin)
            )
        except OSError as e:
            raise IoError(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    load_dotenv()

    port = os.environ.get("PORT", "8080")
    host = os.environ.get("HOST", "127.0.0.1")
    cors_mode = os.environ.get("CORS_MODE", "same-origin")

    security_config = SecurityConfig.new()
    cors_config = CorsConfig.new()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, int(port)))
        listener.listen()
    except OSError as e:
        print(f"Failed to bind to {host}:{port}: {e}")
        raise IoError(e)

    print(f"🖥 Server running on http://{host}:{port}")
    print(
        f"🔒 Security — max content: "
        f"{security_config.max_content_length // const.ONE_KILO_BYTE}KB, "
        f"timeout: {int(security_config.connection_timeout)}s"
    )
    print(f"🌐 CORS mode: {cors_mode}")
    print(f"🛑 Rate limiting: {security_config.max_requests_per_hour} req/hour per IP")

    if DIST_DIR.exists():
        print(f"✨ Serving from {DIST_DIR}/")
    elif PAGES_DIR.exists():
        print(f"📄 Serving from {PAGES_DIR}/ (no dist/ found)")
    else:
        print("⚠️  Warning: neither dist/ nor pages/ found")

    pool = ThreadPool(4, cors_config, security_config)

    try:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError as e:
                print(f"Connection failed: {e}")
                continue
            try:
                pool.execute(conn)
            except ServerError as e:
                print(f"Failed to enqueue connection: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        pool.shutdown()
        listener.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    try:
        main()
    except ServerError as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
