#!/usr/bin/env python3
"""
MasterHttpRelayVPN — VPS Exit Node Server  (Linux only) — Performance Edition

Improvements over original:
  - HTTP connection pool  → TCP handshake فقط یک‌بار per host
  - ThreadPoolExecutor    → thread از قبل آماده، بدون overhead ساخت
  - TCP_NODELAY / KEEPALIVE / buffer tuning
  - connect timeout جدا از read timeout
  - تمام پارامترها از ENV قابل تنظیم

Traffic path:
  Browser → Local Proxy → Apps Script (Google) → THIS SERVER → Target website

ENV variables:
  EXIT_NODE_PSK              required   pre-shared key
  EXIT_NODE_PORT             8181       listen port
  EXIT_NODE_HOST             0.0.0.0    bind address
  EXIT_NODE_WORKERS          50         thread pool size
  EXIT_NODE_CONNECT_TIMEOUT  5          TCP connect timeout (seconds)
  EXIT_NODE_READ_TIMEOUT     25         response read timeout (seconds)
  EXIT_NODE_POOL_SIZE        20         max kept-alive connections per host
  EXIT_NODE_MAX_REQUEST_MB   32         max inbound body (MiB)
  EXIT_NODE_MAX_RESPONSE_MB  64         max outbound body (MiB)
  EXIT_NODE_LOG_LEVEL        INFO       DEBUG / INFO / WARNING / ERROR
"""

import argparse
import base64
import http.client
import http.server
import json
import logging
import os
import queue
import re
import socket
import socketserver
import ssl
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("exit-node")

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

# ---------------------------------------------------------------------------
# Constants — all overridable via ENV
# ---------------------------------------------------------------------------

_PSK: str = ""                                               # set in main()

_WORKERS         = _env_int("EXIT_NODE_WORKERS",          50)
_CONNECT_TIMEOUT = _env_int("EXIT_NODE_CONNECT_TIMEOUT",   5)
_READ_TIMEOUT    = _env_int("EXIT_NODE_READ_TIMEOUT",      25)
_POOL_SIZE       = _env_int("EXIT_NODE_POOL_SIZE",         20)
_MAX_REQUEST_BODY  = _env_int("EXIT_NODE_MAX_REQUEST_MB",  32) * 1024 * 1024
_MAX_RESPONSE_BODY = _env_int("EXIT_NODE_MAX_RESPONSE_MB", 64) * 1024 * 1024

# ---------------------------------------------------------------------------
# Headers that must never reach the upstream target
# ---------------------------------------------------------------------------

_STRIP_HEADERS = frozenset([
    "host", "connection", "content-length", "transfer-encoding",
    "keep-alive", "te", "trailer", "upgrade", "proxy-connection",
    "proxy-authorization", "proxy-authenticate", "x-forwarded-for",
    "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port",
    "x-real-ip", "forwarded", "via", "accept-encoding",
])

# ---------------------------------------------------------------------------
# Connection Pool
# ---------------------------------------------------------------------------

class _ConnectionPool:
    """
    Thread-safe HTTP/HTTPS keep-alive connection pool.

    یک Queue per (scheme, host, port) نگه می‌داره.
    اگه connection خراب بود، یه connection جدید می‌سازه و یک‌بار retry می‌کنه.
    """

    def __init__(self, maxsize: int = 20, connect_timeout: int = 5,
                 read_timeout: int = 25) -> None:
        self._maxsize         = maxsize
        self._connect_timeout = connect_timeout
        self._read_timeout    = read_timeout
        self._pools: dict     = {}
        self._lock            = threading.Lock()
        self._ssl_ctx         = ssl.create_default_context()

    def _pool_for(self, key: tuple) -> queue.Queue:
        with self._lock:
            if key not in self._pools:
                self._pools[key] = queue.Queue(maxsize=self._maxsize)
            return self._pools[key]

    def _new_conn(self, scheme: str, host: str, port: int):
        kwargs = {"timeout": self._read_timeout}
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port,
                                               context=self._ssl_ctx, **kwargs)
        else:
            conn = http.client.HTTPConnection(host, port, **kwargs)
        conn._connect_timeout = self._connect_timeout
        return conn

    def _connect_with_timeout(self, conn) -> None:
        """Open socket with connect timeout, then switch to read timeout."""
        conn.sock = socket.create_connection(
            (conn.host, conn.port),
            timeout=conn._connect_timeout,
            source_address=conn.source_address,
        )
        if isinstance(conn, http.client.HTTPSConnection):
            conn.sock = conn._context.wrap_socket(
                conn.sock, server_hostname=conn.host
            )
        conn.sock.settimeout(conn.timeout)   # switch to read timeout

    def _do_request(self, conn, method: str, path: str,
                    host: str, headers: dict, body: bytes) -> dict:
        req_headers = dict(headers)
        req_headers["Host"]       = host
        req_headers["Connection"] = "keep-alive"
        if body:
            req_headers["Content-Length"] = str(len(body))

        if conn.sock is None:
            self._connect_with_timeout(conn)

        conn.request(method, path, body=body or None, headers=req_headers)
        resp = conn.getresponse()

        data         = resp.read(_MAX_RESPONSE_BODY)
        resp_headers = _collect_headers(resp)
        status       = resp.status

        # Check if server wants to close the connection
        connection_hdr = (resp_headers.get("Connection") or
                          resp_headers.get("connection") or "").lower()
        if resp.version == 10 or "close" in connection_hdr:
            conn.close()

        return {
            "s": status,
            "h": resp_headers,
            "b": base64.b64encode(data).decode(),
        }

    def request(self, method: str, url: str, headers: dict, body: bytes) -> dict:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.lower()
        host   = parsed.hostname or ""
        port   = parsed.port or (443 if scheme == "https" else 80)
        path   = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        key  = (scheme, host, port)
        pool = self._pool_for(key)

        conn = None
        try:
            conn = pool.get_nowait()
        except queue.Empty:
            conn = self._new_conn(scheme, host, port)

        try:
            result = self._do_request(conn, method, path, host, headers, body)
        except Exception:
            # Connection stale — close and retry once with a fresh one
            try:
                conn.close()
            except Exception:
                pass
            conn = self._new_conn(scheme, host, port)
            result = self._do_request(conn, method, path, host, headers, body)

        # Return to pool if socket still alive
        if conn.sock is not None:
            try:
                pool.put_nowait(conn)
            except queue.Full:
                conn.close()
        else:
            try:
                conn.close()
            except Exception:
                pass

        return result


_POOL = None   # initialized in main()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_headers(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not k or not isinstance(k, str):
            continue
        if k.lower() in _STRIP_HEADERS:
            continue
        out[k] = str(v) if v is not None else ""
    return out


def _safe_url(url: str) -> bool:
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return False
    host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    _PRIVATE = re.compile(
        r"^("
        r"localhost"
        r"|127\.\d+\.\d+\.\d+"
        r"|::1"
        r"|0\.0\.0\.0"
        r"|10\.\d+\.\d+\.\d+"
        r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"
        r"|192\.168\.\d+\.\d+"
        r"|169\.254\.\d+\.\d+"
        r"|fc[0-9a-f]{2}:.*"
        r"|fd[0-9a-f]{2}:.*"
        r")$"
    )
    return not _PRIVATE.match(host)


def _collect_headers(resp: http.client.HTTPResponse) -> dict:
    """Preserve duplicate header names (e.g. multiple Set-Cookie lines)."""
    out     = {}
    key_map = {}
    for k, v in resp.getheaders():
        kl = k.lower()
        if kl not in key_map:
            key_map[kl] = k
            out[k]      = v
        else:
            canonical = key_map[kl]
            cur       = out[canonical]
            if isinstance(cur, list):
                cur.append(v)
            else:
                out[canonical] = [cur, v]
    return out

# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _ExitNodeHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args) -> None:
        pass   # we emit our own structured log lines

    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:   # noqa: N802
        self._send_json(200, {
            "ok":      True,
            "status":  "healthy",
            "message": "VPS exit node is running.",
            "workers": _WORKERS,
            "pool":    _POOL_SIZE,
        })

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0:
            self._send_json(400, {"e": "empty_body"})
            return
        if content_length > _MAX_REQUEST_BODY:
            self._send_json(413, {"e": "request_too_large"})
            return

        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except Exception:
            self._send_json(400, {"e": "bad_json"})
            return

        if not isinstance(body, dict):
            self._send_json(400, {"e": "bad_json"})
            return

        k   = str(body.get("k") or "")
        u   = str(body.get("u") or "")
        m   = str(body.get("m") or "GET").upper()
        h   = _sanitize_headers(body.get("h"))
        b64 = body.get("b")

        if not _PSK:
            self._send_json(500, {"e": "server_psk_missing"})
            return

        if k != _PSK:
            log.warning("Unauthorized from %s", self.client_address[0])
            self._send_json(401, {"e": "unauthorized"})
            return

        if not _safe_url(u):
            self._send_json(400, {"e": "bad_url"})
            return

        payload_bytes = b""
        if isinstance(b64, str) and b64:
            try:
                payload_bytes = base64.b64decode(b64)
            except Exception:
                self._send_json(400, {"e": "bad_base64"})
                return

        log.info("Relay %s %s", m, u[:100])
        try:
            result = _POOL.request(m, u, h, payload_bytes)
        except Exception as exc:
            log.warning("Relay error %s: %s", u[:80], exc)
            self._send_json(500, {"e": str(exc) or type(exc).__name__})
            return

        log.info("OK %s → HTTP %d (%d B)", u[:80], result["s"],
                 len(result.get("b", "")))
        self._send_json(200, result)

# ---------------------------------------------------------------------------
# Thread-pool TCP server
# ---------------------------------------------------------------------------

class _ThreadPoolServer(socketserver.TCPServer):
    """
    TCPServer با ThreadPoolExecutor به جای thread-per-request.
    Threadها از قبل آماده‌اند — overhead ساخت thread per request حذف میشه.
    """

    allow_reuse_address = True

    def __init__(self, server_address: tuple, handler_class, workers: int) -> None:
        super().__init__(server_address, handler_class)
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="relay-worker",
        )
        self._tune_socket()

    def _tune_socket(self) -> None:
        s = self.socket
        # کاهش latency — غیرفعال کردن Nagle algorithm
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY,  1)
        # Keep-alive روی connection های ورودی
        s.setsockopt(socket.SOL_SOCKET,  socket.SO_KEEPALIVE, 1)
        # Buffer بزرگ‌تر — کمتر system call در throughput بالا
        s.setsockopt(socket.SOL_SOCKET,  socket.SO_RCVBUF, 262144)  # 256 KiB
        s.setsockopt(socket.SOL_SOCKET,  socket.SO_SNDBUF, 262144)
        # Linux-specific keepalive knobs
        if hasattr(socket, "TCP_KEEPIDLE"):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  30)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,    3)
        log.debug("TCP tuning applied (NODELAY, KEEPALIVE, 256K buffers)")

    def process_request(self, request, client_address) -> None:
        self._executor.submit(
            self.process_request_thread, request, client_address
        )

    def process_request_thread(self, request, client_address) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self) -> None:
        self._executor.shutdown(wait=False)
        super().server_close()

# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="MasterHttpRelayVPN — VPS Exit Node (Performance Edition)",
    )
    parser.add_argument("--psk",       default="",    metavar="SECRET",
                        help="Pre-shared key (or EXIT_NODE_PSK env var).")
    parser.add_argument("--host",      default="0.0.0.0",
                        help="Bind address (or EXIT_NODE_HOST env var).")
    parser.add_argument("--port",      type=int, default=8181,
                        help="TCP port (or EXIT_NODE_PORT env var).")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log verbosity (or EXIT_NODE_LOG_LEVEL env var).")
    args = parser.parse_args()

    log_level = os.environ.get("EXIT_NODE_LOG_LEVEL", args.log_level).upper()
    logging.getLogger().setLevel(log_level)

    if sys.platform != "linux":
        log.error("Linux only. Current platform: %s", sys.platform)
        sys.exit(1)

    global _PSK, _POOL
    _PSK = (args.psk or os.environ.get("EXIT_NODE_PSK", "")).strip()
    if not _PSK:
        log.error("No PSK configured. Pass --psk SECRET or set EXIT_NODE_PSK.")
        sys.exit(1)

    _POOL = _ConnectionPool(
        maxsize         = _POOL_SIZE,
        connect_timeout = _CONNECT_TIMEOUT,
        read_timeout    = _READ_TIMEOUT,
    )

    host = os.environ.get("EXIT_NODE_HOST", args.host)
    port = _env_int("EXIT_NODE_PORT", args.port)

    log.info(
        "Starting — host=%s port=%d workers=%d "
        "connect_to=%ds read_to=%ds pool=%d",
        host, port, _WORKERS, _CONNECT_TIMEOUT, _READ_TIMEOUT, _POOL_SIZE,
    )

    server = _ThreadPoolServer((host, port), _ExitNodeHandler, workers=_WORKERS)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()