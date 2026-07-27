"""Minimal stdlib-only HTTP routing on top of BaseHTTPRequestHandler.

Kept deliberately small: cosmo bundling requires pure-stdlib, so no
framework (aiohttp/flask/etc) is available. Routes are registered per
method+path-regex and matched in order.
"""

import json
import re
import socketserver
import struct
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BufferedIOBase
from typing import Any

import udockerd

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

    @property
    def path(self) -> str:
        return self._handler.path

    def read_json(self) -> Any:
        length = int(self._handler.headers.get("Content-Length", 0) or 0)
        if not length:
            return None
        body = self._handler.rfile.read(length)
        return json.loads(body) if body else None

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._handler.send_response(status)
        self._handler.send_header("Content-Type", "application/json")
        self._handler.send_header("Content-Length", str(len(body)))
        self._handler.end_headers()
        self._handler.wfile.write(body)

    def send_empty(self, status: int) -> None:
        self._handler.send_response(status)
        self._handler.send_header("Content-Length", "0")
        self._handler.end_headers()

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
        # Otherwise BaseHTTPRequestHandler tries to parse a next request
        # off the now-raw socket, and clients reading until EOF hang.
        self._handler.close_connection = True


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
