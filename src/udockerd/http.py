"""Minimal stdlib-only HTTP routing on top of BaseHTTPRequestHandler.

Kept deliberately small: cosmo bundling requires pure-stdlib, so no
framework (aiohttp/flask/etc) is available. Routes are registered per
method+path-regex and matched in order.
"""

import contextlib
import json
import re
import socket
import socketserver
import struct
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BufferedIOBase
from pathlib import Path
from typing import Any, BinaryIO

import udockerd

_COPY_BUF_SIZE = 65536

RouteHandler = Callable[["RequestContext"], None]
Route = tuple[str, "re.Pattern[str]", RouteHandler]

STREAM_STDOUT = 1
STREAM_STDERR = 2


def stream_frame(stream_type: int, payload: bytes) -> bytes:
    """Docker's multiplexed stream framing for non-TTY hijacked
    connections: 8-byte header (1 byte stream type, 3 reserved, 4-byte
    big-endian length) + payload. TTY sessions skip this (raw passthrough).
    """
    return struct.pack(">BxxxI", stream_type, len(payload)) + payload

# Docker CLI/SDK prefix requests with an API version, e.g. "/v1.41/version".
_VERSION_PREFIX = re.compile(r"^/v[0-9]+\.[0-9]+(?=/)")


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: RouteHandler) -> None:
        self._routes.append((method, re.compile(pattern), handler))

    def match(
        self, method: str, path: str
    ) -> tuple[RouteHandler, dict[str, str]] | tuple[None, None]:
        for route_method, pattern, handler in self._routes:
            if route_method != method:
                continue
            m = pattern.match(path)
            if m:
                return handler, m.groupdict()
        return None, None


class RequestContext:
    def __init__(self, handler: BaseHTTPRequestHandler, params: dict[str, str]):
        self._handler = handler
        self.params = params
        # Set once a response line has gone out, so _dispatch's catch-all
        # exception handler knows a fresh send_response(500) would splice
        # a second raw HTTP response into an already-committed body
        # instead of reaching the client as a clean error.
        self.headers_sent = False

    @property
    def path(self) -> str:
        return self._handler.path

    def read_body(self) -> bytes:
        """Raw request body, e.g. a build context tar."""
        if self._handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked_body()
        length = int(self._handler.headers.get("Content-Length", 0) or 0)
        if not length:
            return b""
        return self._handler.rfile.read(length)

    def _read_chunked_body(self) -> bytes:
        rfile = self._handler.rfile
        chunks = []
        while True:
            size_line = rfile.readline().strip()
            size = int(size_line.split(b";", 1)[0], 16)
            if size == 0:
                while rfile.readline().strip():
                    pass
                break
            chunks.append(rfile.read(size))
            rfile.readline()
        return b"".join(chunks)

    def stream_body_to_file(self, dest: Path) -> None:
        """Writes the request body straight to dest instead of buffering
        the whole thing in memory first (read_body() + write_bytes()) --
        build contexts can be large tarballs, and this runs on
        memory-constrained devices (Termux/Android).
        """
        with open(dest, "wb") as f:
            if self._handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
                self._stream_chunked_body_to_file(f)
                return
            remaining = int(self._handler.headers.get("Content-Length", 0) or 0)
            while remaining > 0:
                chunk = self._handler.rfile.read(min(_COPY_BUF_SIZE, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)

    def _stream_chunked_body_to_file(self, f: BinaryIO) -> None:
        rfile = self._handler.rfile
        while True:
            size_line = rfile.readline().strip()
            size = int(size_line.split(b";", 1)[0], 16)
            if size == 0:
                while rfile.readline().strip():
                    pass
                break
            remaining = size
            while remaining > 0:
                chunk = rfile.read(min(_COPY_BUF_SIZE, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
            rfile.readline()

    def read_json(self) -> Any:
        body = self.read_body()
        return json.loads(body) if body else None

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._handler.send_response(status)
        self._handler.send_header("Content-Type", "application/json")
        self._handler.send_header("Content-Length", str(len(body)))
        self._handler.end_headers()
        self._handler.wfile.write(body)
        self.headers_sent = True

    def send_empty(self, status: int) -> None:
        self._handler.send_response(status)
        self._handler.send_header("Content-Length", "0")
        self._handler.end_headers()
        self.headers_sent = True

    @property
    def wfile(self) -> BufferedIOBase:
        return self._handler.wfile

    @property
    def rfile(self) -> BufferedIOBase:
        return self._handler.rfile

    def start_streaming(self, status: int, headers: dict[str, str]) -> None:
        self._handler.send_response(status)
        for key, value in headers.items():
            self._handler.send_header(key, value)
        self._handler.end_headers()
        self.headers_sent = True

    @property
    def headers(self) -> Any:
        return self._handler.headers

    def is_upgrade_request(self) -> bool:
        """True if the client asked for the raw-stream hijack (docker
        exec/attach without Detach): Connection: Upgrade + Upgrade: tcp.
        """
        return (
            self._handler.headers.get("Connection", "").lower() == "upgrade"
            and self._handler.headers.get("Upgrade", "").lower() == "tcp"
        )

    def start_hijack(self) -> None:
        """101 UPGRADED; connection becomes a raw duplex byte stream
        (read/write via ctx.rfile/wfile directly, no more HTTP framing).
        """
        self._handler.send_response_only(101, "UPGRADED")
        self._handler.send_header("Content-Type", "application/vnd.docker.raw-stream")
        self._handler.send_header("Connection", "Upgrade")
        self._handler.send_header("Upgrade", "tcp")
        self._handler.end_headers()
        self.headers_sent = True
        # Otherwise BaseHTTPRequestHandler tries to parse a next request
        # off the now-raw socket, and clients reading until EOF hang.
        self._handler.close_connection = True

    def shutdown_read(self) -> None:
        """Unblocks a thread stuck in a blocking rfile.read() (TTY stdin
        forwarding, container_proc.stream_session) once the session is
        over. Plain rfile.close() deadlocks here: BufferedReader.close()
        needs the same internal per-object lock a concurrent in-progress
        read() call is holding, so it blocks forever waiting for a read
        that's waiting for bytes nobody will send. Shutting down the raw
        socket instead interrupts that read (returns b"") without
        touching the buffered wrapper's lock.
        """
        with contextlib.suppress(OSError):
            self._handler.connection.shutdown(socket.SHUT_RDWR)


def make_handler_class(router: Router) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"udockerd/{udockerd.__version__}"
        # Default HTTP/1.0 makes the docker CLI's client hang on hijack responses.
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            path = _VERSION_PREFIX.sub("", path) or "/"
            handler, params = router.match(method, path)
            if handler is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ctx = RequestContext(self, params or {})
            try:
                handler(ctx)
            except Exception as exc:  # noqa: BLE001 - surface as Docker-API-shaped error
                if ctx.headers_sent:
                    # A response (or the start of a stream) already went
                    # out; a fresh send_response(500) here wouldn't reach
                    # the client as an HTTP response; it'd splice a second
                    # raw status line into the body of the first one,
                    # corrupting it. Best effort at this point is to just
                    # drop the connection instead of a malformed stream.
                    self.close_connection = True
                    return
                body = json.dumps({"message": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    return Handler


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
