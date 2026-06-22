"""
MY CUSTOM PYTHON SERVER: NOX_SERVER.
Direct, structural replica of main.rs. Process documented as needed.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
import queue
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import nox_server as nox
from nox_server import HttpResponse, RateLimiter
import constants as const


def load_dotenv(path: str = ".env") -> None:
    """
    Minimal stdlib-only .env loader.

    main.rs does not load a .env file itself either — std::env::var only
    reads variables already present in the process environment. Whatever
    mechanism feeds .env values into the Rust binary on your machine
    (an IDE launch config, direnv, a shell that sources .env, etc.) sits
    outside main.rs/lib.rs and isn't something Python inherits for free.
    This function plays that same external role explicitly, so running
    `python3 main.py` from a plain terminal behaves the same way.

    Rules, matching standard .env convention:
      - Lines starting with '#' or blank lines are skipped.
      - KEY=VALUE pairs are parsed; surrounding whitespace is stripped.
      - Quoted values ('...' or "...") have the quotes removed.
      - Variables ALREADY set in the real environment are never
        overwritten — an explicit `export FOO=bar` in your shell always
        takes priority over the .env file, same as most .env tooling.
      - If the file doesn't exist, this is a silent no-op (mirrors
        dotenv's typical .ok()/best-effort behavior).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return  # No .env file — nothing to do.

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # Strip matching surrounding quotes, if present
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Connection and Rate Limiting Configurations
# ---------------------------------------------------------------------------
@dataclass
class SecurityConfig:
    """
    Replica of Rust's SecurityConfig (#[derive(Clone)]).

    Python dataclasses are copyable by construction (no special Clone trait
    needed) — `dataclasses.replace()` is the structural equivalent of
    Rust's `.clone()` if a copy is ever needed, though this codebase
    only ever reads from instances after creation (matching Rust's usage).
    """

    max_content_length: int
    max_connections: int
    connection_timeout: float  # seconds (Rust: Duration)
    max_requests_per_hour: int
    enable_rate_limiting: bool

    @staticmethod
    def new() -> "SecurityConfig":
        return SecurityConfig(
            max_content_length=const.MAX_CONTENT_LENGTH,  # 50KB instead of 10KB
            max_connections=100,
            connection_timeout=30.0,
            max_requests_per_hour=100,  # 100 requests per hour per IP
            enable_rate_limiting=True,
        )


# ---------------------------------------------------------------------------
# Route enum
# ---------------------------------------------------------------------------
class Route(Enum):
    """Replica of Rust's `enum Route`."""

    STATIC = auto()
    CONTACT_FORM = auto()
    API_STATUS = auto()
    API_HEALTH = auto()
    NOT_FOUND = auto()


def match_route(method: str, path: str) -> Route:
    """
    Replica of Rust's match_route, using match (method, path) tuple patterns.
    Python's match statement (3.10+) mirrors this almost verbatim.
    """
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
# Custom error types for better error handling
# ---------------------------------------------------------------------------
class ServerError(Exception):
    """
    Base replica of Rust's `enum ServerError`.

    Rust's enum variants (IoError, ParseError, ValidationError, EmailError,
    ThreadPoolError, NetworkError) are each represented as a Python
    Exception subclass below. This mirrors Rust's tagged-union error type
    with Python's native exception hierarchy — `isinstance` checks replace
    `match`-on-variant, and `str(err)` replaces the `Display` impl.
    """

    def __str__(self) -> str:
        return self._display()

    def _display(self) -> str:
        return super().__str__()


class IoError(ServerError):
    def __init__(self, e: Exception) -> None:
        self.inner = e
        super().__init__(str(e))

    def _display(self) -> str:
        return f"IO error: {self.inner}"


class ParseError(ServerError):
    def __init__(self, msg: str) -> None:
        self.msg = msg
        super().__init__(msg)

    def _display(self) -> str:
        return f"Parse error: {self.msg}"


class ValidationError(ServerError):
    def __init__(self, msg: str) -> None:
        self.msg = msg
        super().__init__(msg)

    def _display(self) -> str:
        return f"Validation error: {self.msg}"


class EmailError(ServerError):
    def __init__(self, e: Exception) -> None:
        self.inner = e
        super().__init__(str(e))

    def _display(self) -> str:
        return f"Email error: {self.inner}"


class ThreadPoolError(ServerError):
    def __init__(self, msg: str) -> None:
        self.msg = msg
        super().__init__(msg)

    def _display(self) -> str:
        return f"Thread pool error: {self.msg}"


class NetworkError(ServerError):
    def __init__(self, msg: str) -> None:
        self.msg = msg
        super().__init__(msg)

    def _display(self) -> str:
        return f"Network error: {self.msg}"


# Rust: type ServerResult<T> = Result<T, ServerError>;
# Python has no Result alias to mirror; ServerError subclasses are raised
# and caught with try/except instead. Function signatures are annotated
# with the success type only (-> None, -> int, etc.) and a docstring notes
# what they raise, which is the idiomatic Python equivalent.


# ---------------------------------------------------------------------------
# CORS Configuration
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
        elif cors_mode == "same-origin":
            return CorsConfig.same_origin_config()
        else:
            return CorsConfig.same_origin_config()

    @staticmethod
    def cross_origin_config() -> "CorsConfig":
        return CorsConfig(
            allow_origins=[
                "http://localhost:5173",       # React dev server
                "https://nox-dev.vercel.app",  # Production frontend
            ],
            allow_all_origins=False,  # Set to True for development only
            allow_methods=["GET", "POST", "OPTIONS"],  # Keep OPTIONS for preflight requests
            allow_headers=[
                "Content-Type",
                "Authorization",
                "X-Requested-With",
                "Accept",
                "Origin",
                "ngrok-skip-browser-warning",
            ],
            max_age=const.CORS_CONFIG_MAX_AGE,  # 24 hours
        )

    @staticmethod
    def same_origin_config() -> "CorsConfig":
        return CorsConfig(
            allow_origins=[],  # Same origin doesn't need explicit origins
            allow_all_origins=False,
            allow_methods=["GET", "POST", "OPTIONS"],  # Keep OPTIONS for preflight requests
            allow_headers=["Content-Type", "Accept"],
            max_age=const.CORS_CONFIG_MIN_AGE,  # 1 hour
        )

    def is_method_allowed(self, method: str) -> bool:
        return method.upper() in self.allow_methods

    def is_origin_allowed(self, origin: str) -> bool:
        if self.allow_all_origins:
            return True
        if not self.allow_origins:
            return True  # Same-origin mode
        return origin in self.allow_origins


# ---------------------------------------------------------------------------
# ThreadPool to include rate limiter and security config
# ---------------------------------------------------------------------------
class Worker:
    """
    Replica of Rust's Worker.

    Rust: thread::spawn(move || loop { receiver.lock().unwrap().recv() ... })
    Python: a daemon-less threading.Thread running the same blocking loop,
    pulling from a shared queue.Queue. queue.Queue is the direct structural
    analogue of mpsc::Receiver<T> wrapped in Arc<Mutex<_>> — it is already
    internally synchronized for multi-consumer use, so no extra lock is
    layered on top (Python's queue module exists specifically to replace
    that exact Mutex<Receiver> pattern).

    A sentinel value (`None`) is pushed onto the queue at shutdown to
    unblock `queue.get()`, replicating Rust's behavior where dropping the
    Sender causes `recv()` to return `Err(_)` and the worker loop to break.
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
                # Sentinel: equivalent to mpsc recv() returning Err on closed channel
                print(f"Worker {self.id} shutting down")
                break
            try:
                handle_connection_safe(stream, cors_config, rate_limiter, security_config)
            except ServerError as e:
                print(f"Worker {self.id} encountered error: {e}")
            except Exception as e:
                print(f"Worker {self.id} encountered error: {e}")
            finally:
                # Rust's TcpStream is closed automatically via Drop when it
                # goes out of scope at the end of handle_connection_safe.
                # Python sockets have no such automatic close, so it must
                # be done explicitly here — otherwise the browser treats
                # the still-open socket as keep-alive eligible and reuses
                # it for a follow-up request that no worker is listening
                # for anymore, causing the request to hang indefinitely.
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # already closed or peer disconnected — fine
                stream.close()


class ThreadPool:
    """Replica of Rust's ThreadPool."""

    def __init__(
        self,
        size: int,
        cors_config: CorsConfig,
        security_config: SecurityConfig,
    ) -> None:
        if size == 0:
            raise ThreadPoolError("Thread pool size cannot be zero")

        self._queue: "queue.Queue[Optional[socket.socket]]" = queue.Queue()
        self.cors_config = cors_config
        self.security_config = security_config

        # Rate limiter — shared across all workers (Rust: Arc<RateLimiter>)
        self.rate_limiter = RateLimiter(
            security_config.max_requests_per_hour,
            const.WINDOW_LIMIT_MINS,  # 1 hour window
        )

        self.workers: list[Worker] = []
        for worker_id in range(size):
            try:
                worker = Worker(
                    worker_id,
                    self._queue,
                    cors_config,
                    self.rate_limiter,
                    security_config,
                )
                self.workers.append(worker)
            except Exception as e:
                raise ThreadPoolError(f"Failed to create worker {worker_id}: {e}")

        self._closed = False

    def execute(self, stream: socket.socket) -> None:
        """Raises ThreadPoolError on failure (Rust: ServerResult<()>)."""
        try:
            stream.settimeout(self.security_config.connection_timeout)
        except OSError as e:
            print(f"Warning: Could not set socket timeout: {e}")

        try:
            self._queue.put(stream)
        except Exception as e:
            raise ThreadPoolError(f"Failed to send task: {e}")

    def shutdown(self) -> None:
        """
        Replica of Rust's `impl Drop for ThreadPool`.

        Rust's Drop runs automatically when ThreadPool goes out of scope;
        Python has no deterministic destructor equivalent for this kind of
        cleanup (the `__del__` is unreliable and discouraged for resource
        teardown), so this is called explicitly — see main()'s `finally`
        block, which plays the role Rust's automatic scope-exit Drop call
        would play.
        """
        if self._closed:
            return
        self._closed = True

        # Drop sender equivalent: push one sentinel per worker
        for _ in self.workers:
            self._queue.put(None)

        for worker in self.workers:
            print(f"Shutting down worker {worker.id}")
            worker.thread.join()


# ---------------------------------------------------------------------------
# Route handler functions
# ---------------------------------------------------------------------------
def handle_api_status(
    stream: socket.socket,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    timestamp = int(time.time())

    json_response = f'{{"status":"healthy","version":"1.0.0","timestamp":{timestamp}}}'

    origin_header = get_cors_origin_header(cors_config, origin)

    try:
        HttpResponse.ok().json(json_response).send(stream, origin_header)
    except OSError as e:
        raise IoError(e)


def handle_api_health(
    stream: socket.socket,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    origin_header = get_cors_origin_header(cors_config, origin)
    try:
        HttpResponse.ok().text("OK").send(stream, origin_header)
    except OSError as e:
        raise IoError(e)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------
def main() -> None:
    # Load .env into the process environment first, mirroring wherever
    # your Rust project calls dotenv/dotenvy at the top of fn main().
    load_dotenv()

    port = os.environ.get("PORT", "8080")
    host = os.environ.get("HOST", "127.0.0.1")
    addr = (host, int(port))

    # Initialize security configuration
    security_config = SecurityConfig.new()
    cors_config = CorsConfig.new()
    cors_mode = os.environ.get("CORS_MODE", "same-origin")

    dist_exists = Path("../dist").exists()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(addr)
        listener.listen()
    except OSError as e:
        print(f"Failed to bind to address {host}:{port}: {e}")
        raise IoError(e)

    print(f"🦀 Server running on http://{host}:{port}")

    print(
        f"🦍 Security enabled - Max content: "
        f"{security_config.max_content_length // const.ONE_KILO_BYTE}KB, "
        f"Timeout: {int(security_config.connection_timeout)}s"
    )

    print(f"🐊 CORS Mode: {cors_mode}")

    print(f"🦥 Rate limiting: {security_config.max_requests_per_hour} requests/hour per IP")

    if dist_exists:
        print("✨ Serving static files from ../dist/")
    else:
        print("⚠️  Warning: ../dist/ folder not found!")
        print("📜 Serving index.html from current directory as fallback")

    # thread pool with security config
    pool = ThreadPool(4, cors_config, security_config)

    try:
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError as e:
                print(f"Connection failed: {e}")
                continue

            try:
                pool.execute(conn)
            except ServerError as e:
                print(f"Failed to execute task: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        # Rust's ThreadPool::drop runs implicitly at scope exit; here it's
        # invoked explicitly since Python has no equivalent guarantee.
        pool.shutdown()
        listener.close()


def handle_connection_safe(
    stream: socket.socket,
    cors_config: CorsConfig,
    rate_limiter: RateLimiter,
    security_config: SecurityConfig,
) -> None:
    # Fallback IP from the raw socket — this is accurate for direct
    # connections, but when traffic comes through a reverse proxy/tunnel
    # (ngrok, nginx, Cloudflare, etc.) it only ever sees the PROXY's own
    # address, not the real visitor's. Every tunneled visitor would
    # otherwise collapse onto this one IP and share a single rate-limit
    # bucket — exhausting it for one visitor exhausts it for everyone.
    try:
        socket_ip = stream.getpeername()[0]
    except OSError:
        socket_ip = "unknown"

    buf_reader = BufReader(stream)
    request_line = buf_reader.read_line()

    # Add timeout for reading request line
    if request_line == "":
        raise NetworkError("Connection closed by client")

    if request_line.strip() == "":
        raise NetworkError("Empty request line")

    # Limit request line length to prevent DoS
    if len(request_line) > const.MAX_REQUEST_LINE_SIZE:
        send_error_response_with_cors(
            stream,
            "414 Request-URI Too Long",
            "Request line too long",
            cors_config,
            None,
        )
        return

    # Read headers with security limits
    content_length, origin, forwarded_for = parse_headers_secure(buf_reader, security_config)

    # Prefer the real client IP from X-Forwarded-For (set by a trusted
    # proxy in front of us) over the raw socket peer address. This means
    # rate limiting now applies per real-world visitor again, instead of
    # lumping every tunneled visitor into one bucket keyed on the proxy.
    #
    # NOTE ON TRUST: X-Forwarded-For is a client-controllable header in
    # general — anyone could fake it. We only trust it here because this
    # server, when deployed behind ngrok, ONLY receives traffic that has
    # already passed through ngrok's edge, which overwrites this header
    # itself rather than passing through an attacker-supplied value
    # unchanged. If you deploy this directly on the open internet WITHOUT
    # a trusted proxy in front of it, do not trust this header blindly —
    # an attacker could set arbitrary X-Forwarded-For values to spoof
    # their way around per-IP rate limiting.
    client_ip = forwarded_for if forwarded_for else socket_ip

    # Check rate limiting (moved here, after header parsing, since we need
    # X-Forwarded-For from the headers before we know the real client IP)
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

    method = parts[0]
    path = parts[1]

    print(
        f"📥 {method} {path} from {client_ip} "
        f"(Content: {content_length // const.ONE_KILO_BYTE}KB)"
    )

    # Check if method is allowed
    if not cors_config.is_method_allowed(method):
        print(f"🚫 Method {method} not allowed for {client_ip}")
        send_error_response_with_cors(
            stream, "405 Method Not Allowed", "Method not allowed", cors_config, origin
        )
        return

    # Handle OPTIONS first (preflight)
    if method == "OPTIONS":
        handle_preflight_request(stream, cors_config, origin)
        return

    # Route matching
    route = match_route(method, path)

    # Add your routes and make sure it's available in enum Route
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
    elif route == Route.NOT_FOUND:
        origin_header = get_cors_origin_header(cors_config, origin)
        try:
            HttpResponse.not_found().send(stream, origin_header)
        except OSError as e:
            raise IoError(e)


class BufReader:
    """
    Minimal structural replica of Rust's `BufReader<&mut TcpStream>`,
    providing `read_line()` (-> str, like Rust's `read_line` into a
    String) and `read_exact(n)` (-> bytes, like `read_exact` into a
    buffer). Needed because Python sockets have no built-in buffered
    line reader the way Rust's `std::io::BufRead` does.
    """

    def __init__(self, stream: socket.socket, bufsize: int = 4096) -> None:
        self._stream = stream
        self._buf = b""
        self._bufsize = bufsize

    def get_mut(self) -> socket.socket:
        """Rust: buf_reader.get_mut() -> &mut TcpStream"""
        return self._stream

    def read_line(self) -> str:
        """Reads until '\\n' (inclusive) or EOF. Returns '' on immediate EOF, matching Rust's Ok(0) case."""
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
        """Rust: buf_reader.read_exact(&mut body) — reads exactly n bytes or raises IoError."""
        while len(self._buf) < n:
            try:
                chunk = self._stream.recv(self._bufsize)
            except OSError as e:
                raise IoError(e)
            if not chunk:
                raise IoError(
                    OSError("failed to fill whole buffer (connection closed early)")
                )
            self._buf += chunk

        data, self._buf = self._buf[:n], self._buf[n:]
        return data


def parse_headers_secure(
    buf_reader: BufReader,
    security_config: SecurityConfig,
) -> tuple[int, Optional[str], Optional[str]]:
    content_length = 0
    origin: Optional[str] = None
    forwarded_for: Optional[str] = None
    header_count = 0
    max_headers = 50  # Limit number of headers

    while True:
        line = buf_reader.read_line()

        if line.strip() == "":
            break

        header_count += 1
        if header_count > max_headers:
            raise ParseError("Too many headers")

        # Limit header line length
        if len(line) > const.MAX_HEADER_LINE_SIZE:
            raise ParseError("Header line too long")

        line_lower = line.lower()
        if line_lower.startswith("content-length:"):
            split_parts = line.split(": ", 1)
            if len(split_parts) > 1:
                length_str = split_parts[1]
                try:
                    content_length = int(length_str.strip())
                except ValueError:
                    raise ParseError("Invalid content-length")

                # CRITICAL: Check content length against security limits
                if content_length > security_config.max_content_length:
                    raise ValidationError(
                        f"Content length {content_length} exceeds maximum allowed "
                        f"{security_config.max_content_length}"
                    )
        elif line_lower.startswith("origin:"):
            split_parts = line.split(": ", 1)
            if len(split_parts) > 1:
                origin_trimmed = split_parts[1].strip()
                # Limit origin header length
                if len(origin_trimmed) > 200:
                    raise ParseError("Origin header too long")
                origin = origin_trimmed
        elif line_lower.startswith("x-forwarded-for:"):
            # Set by reverse proxies (ngrok, nginx, Cloudflare, etc.) to
            # carry the real client IP, since stream.getpeername() only
            # ever sees the proxy's own address once traffic is tunneled.
            # Format can be a comma-separated chain ("client, proxy1, proxy2")
            # if multiple proxies are involved — the FIRST entry is the
            # original client, which is the one we want for rate limiting.
            split_parts = line.split(": ", 1)
            if len(split_parts) > 1:
                xff_value = split_parts[1].strip()
                if len(xff_value) > 200:
                    raise ParseError("X-Forwarded-For header too long")
                first_ip = xff_value.split(",")[0].strip()
                if first_ip:
                    forwarded_for = first_ip

    return content_length, origin, forwarded_for


def handle_preflight_request(
    stream: socket.socket,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    origin_header = get_cors_origin_header(cors_config, origin)

    # CRITICAL: Block unauthorized origins at preflight
    if origin_header == "" and origin is not None:
        response = "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
        stream.sendall(response.encode("utf-8"))
        print(nox.red(f"⛔️ Blocked preflight from unauthorized origin: {origin!r}", False))
        return

    # Only send CORS details if origin is allowed
    response = (
        f"HTTP/1.1 200 OK\r\n{origin_header}\r\n"
        f"Access-Control-Allow-Methods: {', '.join(cors_config.allow_methods)}\r\n"
        f"Access-Control-Allow-Headers: {', '.join(cors_config.allow_headers)}\r\n"
        f"Access-Control-Max-Age: {cors_config.max_age}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )

    stream.sendall(response.encode("utf-8"))
    print(f" Sent preflight response for origin: {origin!r}")


def get_cors_origin_header(cors_config: CorsConfig, origin: Optional[str]) -> str:
    if origin is not None:
        if cors_config.is_origin_allowed(origin):
            if cors_config.allow_all_origins:
                return "Access-Control-Allow-Origin: *"
            else:
                return (
                    f"Access-Control-Allow-Origin: {origin}\r\n"
                    f"Access-Control-Allow-Credentials: true"
                )
        else:
            # Don't send CORS headers for disallowed origins
            return ""
    else:
        # Same-origin request or no origin header
        if cors_config.allow_all_origins:
            return "Access-Control-Allow-Origin: *"
        else:
            return ""


def serve_static_file_with_cors(
    stream: socket.socket,
    path: str,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    # Check if dist folder exists
    dist_exists = Path("../dist").exists()

    if not dist_exists:
        # Serve index.html from current directory as fallback
        serve_hello_file(stream, cors_config, origin)
        return

    safe_path = nox.sanitize_path(path)
    file_path = Path("../dist") / safe_path

    if not nox.is_safe_path(file_path):
        send_error_response_with_cors(stream, "403 Forbidden", "Access denied", cors_config, origin)
        return

    if safe_path == "" or safe_path == "/":
        final_path = Path("../dist/index.html")
    else:
        final_path = file_path

    try:
        content = final_path.read_bytes()
        mime_type = nox.get_mime_type(final_path)
        send_file_response_with_cors(stream, content, mime_type, cors_config, origin)
    except OSError:
        send_error_response_with_cors(stream, "404 Not Found", "File not found", cors_config, origin)


def serve_hello_file(
    stream: socket.socket,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    index_path = Path.cwd() / "pages" / "index.html"

    try:
        content = index_path.read_bytes()
        print("Serving index.html from current directory")
        send_file_response_with_cors(stream, content, "text/html", cors_config, origin)
    except OSError:
        print("🔎 Warning: index.html not found in current directory, resolve to fallback")
        # Create a basic HTML response if index.html is also missing
        fallback_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nonso Martin | Server Running</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
        .status { color: #28a745; }
        .warning { color: #ffc107; background: #fff3cd; padding: 10px; border-radius: 4px; }
    </style>
</head>
<body>
    <h1 class="status">Python Server is Running!</h1>
    <div class="warning">
        <strong>Note:</strong> Neither <code>../dist/</code> folder nor <code>index.html</code> were found.
        <br>This is a fallback page to confirm the server is working.
    </div>
    <p>Server is ready to:</p>
    <ul>
        <li>Handle POST requests</li>
        <li>Serve static files (when available)</li>
        <li>Process form submissions</li>
    </ul>
</body>
</html>"""
        send_file_response_with_cors(
            stream, fallback_html.encode("utf-8"), "text/html", cors_config, origin
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


# ReDoS-resistant email regex - avoids catastrophic backtracking.
# Compiled once at module load, mirroring Rust's Regex::new being called
# inside validate_form_data each invocation (Rust's `regex` crate
# internally caches/compiles efficiently per-call in the original; here
# it's hoisted to module scope, which is the idiomatic Python equivalent
# of "compile once" and avoids needless recompilation per request).
_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def validate_form_data(form_data: dict[str, str]) -> None:
    """Improved form validation with ReDoS-resistant email regex. Raises ValidationError on failure."""
    allowed_fields = {c.name for c in const.FIELD_CONSTRAINTS}

    # Check for unexpected fields
    unexpected_fields = [k for k in form_data.keys() if k not in allowed_fields]

    if unexpected_fields:
        raise ValidationError(f"Unexpected fields found: {unexpected_fields!r}")

    # Validate required fields and constraints
    errors: list[str] = []

    for constraint in const.FIELD_CONSTRAINTS:
        value = form_data.get(constraint.name)
        if value is not None:
            # Check for null bytes and control characters.
            # NOTE: Python's str.isprintable() is NOT the inverse of Rust's
            # char::is_control() — isprintable() also flags things like
            # U+200B (zero-width space) and non-breaking space as
            # "non-printable", which Rust's is_control() does NOT consider
            # control characters. unicodedata category 'Cc' is the correct
            # match for Rust's is_control() (Unicode General Category "Cc").
            has_bad_control_char = any(
                unicodedata.category(c) == "Cc" and c not in ("\n", "\r", "\t")
                for c in value
            )
            if "\0" in value or has_bad_control_char:
                errors.append(f"{constraint.name} contains invalid characters")
                continue

            # Length validation
            if len(value) > constraint.max_length:
                errors.append(
                    f"{constraint.name} too long (max {constraint.max_length} characters, got {len(value)})"
                )

            # Email specific validation
            if constraint.email:
                # Additional length check for email before regex
                if len(value) > const.RFC_5321_MAX_EMAIL_LENGTH:
                    errors.append("Email address too long")
                elif not _EMAIL_REGEX.match(value):
                    errors.append("Invalid email format")

                # Additional email security checks
                local_domain = value.split("@")
                if len(local_domain) == 2:
                    local_part, domain_part = local_domain

                    if len(local_part) > const.RFC_5321_MAX_LOCAL_PART_LENGTH:
                        errors.append("Email local part too long")
                    if len(domain_part) > const.RFC_5321_MAX_DOMAIN_PART_LENGTH:
                        errors.append("Email domain too long")

            # Content validation for message field
            if constraint.name == "message":
                # Check for suspicious patterns that might indicate spam
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

                value_lower = value.lower()
                suspicious_count = sum(
                    1 for pattern in suspicious_patterns if pattern in value_lower
                )

                if suspicious_count > 2:
                    errors.append("Message contains suspicious content")
        else:
            if constraint.required:
                errors.append(f"Missing required field: {constraint.name}")

    # Validate checkbox fields: only "on" is allowed if present
    for cb_name in const.OPTIONAL_CHECKBOX:
        cb_value = form_data.get(cb_name)
        if cb_value is not None and cb_value != "on":
            errors.append(f"Invalid value for checkbox '{cb_name}'")

    # Additional security validations
    total_content_size = sum(len(v) for v in form_data.values())
    if total_content_size > const.MAX_FORM_DATA_LENGTH:
        # 10KB total form data limit
        errors.append("Total form data too large")

    # Check for potential XSS attempts in any field
    for field_name, value in form_data.items():
        if nox.contains_potential_xss(value):
            errors.append(f"Field '{field_name}' contains potentially malicious content")

    if errors:
        raise ValidationError(", ".join(errors))


def handle_post_request_secure(
    buf_reader: BufReader,
    content_length: int,
    cors_config: CorsConfig,
    origin: Optional[str],
    security_config: SecurityConfig,
    client_ip: str,
) -> None:
    if content_length == 0:
        send_html_response_with_cors_secure(
            buf_reader, "Error", "No data received", None, None, cors_config, origin
        )
        return

    # Use security config limits
    if content_length > security_config.max_content_length:
        print(f"⛔️ Payload too large from {client_ip}: {content_length // 1024}KB")
        send_html_response_with_cors_secure(
            buf_reader, "Error", "Payload too large", None, None, cors_config, origin
        )
        return

    # Validate content_length before allocating memory
    try:
        body = buf_reader.read_exact(content_length)
    except IoError as e:
        print(f"⛔️ Failed to read POST body from {client_ip}: {e}")
        raise

    body_str = body.decode("utf-8", errors="replace")

    print(f"📨 POST data received from {client_ip} ({len(body)}B)")

    if "Content-Disposition: form-data" in body_str:
        form_data = nox.parse_multipart_data(body_str)
    else:
        form_data = nox.parse_form_data(body_str)

    # Validate form data BEFORE proceeding
    try:
        validate_form_data(form_data)
    except ValidationError as e:
        print(f"⛔️ Form validation failed for {client_ip}: {e}")
        send_html_response_with_cors_secure(
            buf_reader, "Error", str(e), None, None, cors_config, origin
        )
        return

    print(f"✅ Form validation passed for {client_ip}")

    # Log sanitized form data
    for key, value in form_data.items():
        if key == "email":
            print(f"📧 Field '{key}': '***@***'")
        elif key == "message":
            preview = "".join(list(value)[:50])
            print(f"📝 Field '{key}': '{preview}' ({len(value)}chars)")
        else:
            print(f"📄 Field '{key}': '{value}'")

    name = form_data.get("name")
    email = form_data.get("email")

    # Send email ONLY after successful validation
    if email is not None:
        email_sent = send_confirmation_email(form_data, email, client_ip)
    else:
        email_sent = False

    # Send success response
    send_html_response_with_cors_secure(
        buf_reader,
        "Success",
        "Form submitted successfully and confirmation email sent!"
        if email_sent
        else "Form submitted successfully!",
        name,
        email,
        cors_config,
        origin,
    )


def send_confirmation_email(
    form_data: dict[str, str],
    email_addr: str,
    client_ip: str,
) -> bool:
    """Helper function for sending confirmation email."""
    user_name = form_data.get("name", "Dear Friend")
    user_message = form_data.get("message", "Your form submission")

    subject = "Thank you for contacting me!"

    # Sanitize email content
    email_addr = nox.sanitize_email_content(email_addr)
    user_name = nox.sanitize_email_content(user_name)
    user_message = nox.sanitize_email_content(user_message)

    # Generate HTML email
    html_body = nox.generate_email_html(user_name, user_message)

    # Fallback plain text version
    plain_text_body = (
        f"Hello {user_name},\n\nThank you for your submission! "
        f"I received your message and will attend to your queries shortly.\n\n"
        f"Your message: {user_message}\n\nBest regards,\nNonso Martin"
    )

    # Try to send HTML email with plain text fallback
    try:
        nox.send_html_email(email_addr, subject, html_body, plain_text_body)
        print(f"✅ Email sent successfully to {email_addr} from {client_ip}")
        return True
    except Exception as e:
        print(f"⚠️ Failed to send HTML email to {email_addr} from {client_ip}: {e}")

        # Fallback to plain text
        try:
            nox.send_email(email_addr, subject, plain_text_body)
            print(f"✅ Fallback plain text email sent to {email_addr} from {client_ip}")
            return True
        except Exception as e2:
            print(f"⛔️ Failed to send any email to {email_addr} from {client_ip}: {e2}")
            return False


def send_html_response_with_cors_secure(
    buf_reader: BufReader,
    status_type: str,
    message: str,
    name: Optional[str],
    email: Optional[str],
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    """Updated HTML response function with security."""
    origin_header = get_cors_origin_header(cors_config, origin)
    cors_headers = f"{origin_header}\r\n" if origin_header else ""

    # Use the secure HTML generation function
    html_content = generate_response_html(status_type, message, name, email)

    response = (
        f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(html_content.encode('utf-8'))}\r\n"
        f"X-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
        f"X-XSS-Protection: 1; mode=block\r\n{cors_headers}\r\n{html_content}"
    )

    stream = buf_reader.get_mut()
    stream.sendall(response.encode("utf-8"))


def generate_response_html(
    status_type: str,
    message: str,
    name: Optional[str],
    email: Optional[str],
) -> str:
    """Fixed generate_response_html function with HTML escaping."""
    if status_type == "Success":
        # HTML escape user inputs
        name_display = nox.html_escape(name if name is not None else "Your name")
        email_display = nox.html_escape(email if email is not None else "your email")

        thank_you_message = (
            f"Thank you <strong>{name_display}</strong>! You will receive a message "
            f"shortly to your designated email: <strong>{email_display}</strong>"
        )

        title, heading, content = (
            "Form Submitted Successfully",
            "🥂 Success!",
            thank_you_message,
        )
    elif status_type == "Error":
        title, heading, content = (
            "Form Submission Error",
            "⛔️ Error",
            f"Sorry, there was an issue: {nox.html_escape(message)}",
        )
    else:
        title, heading, content = ("Form Response", "Response", nox.html_escape(message))

    status_class = "success" if status_type == "Success" else "error"

    return f"""<!DOCTYPE html>
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
    <title>{nox.html_escape(title)}</title>
    <style>
        * {{
            padding: 0;
            margin: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: "Outfit", system-ui, sans-serif;
            max-width: 100vw;
            background-color: #222;
            color: white;
            min-height: 100vh;
            overflow: hidden;
            display: grid;
            place-content: center;
        }}

        .container {{
            background-color: #101010;
            height: 300px;
            width: 500px;
            border-radius: 15px;
            padding: 2% 3%;
            display: flex;
            flex-direction: column;
            justify-content: space-evenly;
        }}

        h1 {{
            color: #fff;
            text-align: center;
            font-size: 2.5em;
        }}

        .message {{
            border-left: 4px solid #34db69;
            padding:2%;
            height: 50%;
            border-radius: 5px;
            font-size: 1.2rem;
            line-height: 1.5;
        }}

        .back-link {{
            display: inline-block;
            width: 40%;
            height: 15%;
            display: grid;
            place-content: center;
            background-color: #222;
            color: white;
            text-decoration: none;
            border-radius: 5px;
            transition-duration: 0.2s;
            border: 1px solid #474646ff;
        }}
        .back-link:hover {{
            background: #34db69ff;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px #34db695b;
        }}
        .error {{
            border-left-color: #fa4646ff;
        }}
        .success {{
            border-left-color: #51cf66;
        }}

        @media screen and (max-width: 510px) {{
            .container {{
                 width: 97dvw !important;
             }}
             .back-link {{
                 width: 60%;
             }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{heading}</h1>
        <div class="message {status_class}">
            {content}
        </div>
        <a href="/" class="back-link">Back to Homepage</a>
    </div>
</body>
</html>"""


def send_error_response_with_cors(
    stream: socket.socket,
    status: str,
    message: str,
    cors_config: CorsConfig,
    origin: Optional[str],
) -> None:
    origin_header = get_cors_origin_header(cors_config, origin)
    cors_headers = f"{origin_header}\r\n" if origin_header else ""

    html_content = (
        f"<!DOCTYPE html><html><head><title>{status}</title></head>"
        f"<body><h1>{status}</h1><p>{message}</p></body></html>"
    )

    response = (
        f"HTTP/1.1 {status}\r\nContent-Type: text/html\r\n"
        f"Content-Length: {len(html_content.encode('utf-8'))}\r\n"
        f"{cors_headers}\r\n{html_content}"
    )

    stream.sendall(response.encode("utf-8"))


if __name__ == "__main__":
    # Force line-buffered output so log lines appear immediately when
    # stdout is redirected to a file (e.g. `nohup python3 main.py > log &`),
    # instead of sitting in Python's internal buffer until it fills or the
    # process exits. Rust's println! flushes on every call by default, so
    # this has no equivalent need in the original — it's specific to this
    # port and matters for anyone launching main.py directly (without the
    # `-u` flag the server-manager.sh script already passes).
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    try:
        main()
    except ServerError as e:
        print(f"Fatal server error: {e}", file=sys.stderr)
        sys.exit(1)