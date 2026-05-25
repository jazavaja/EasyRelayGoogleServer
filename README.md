# EasyRelayGoogle — VPS Exit Node

A lightweight HTTP relay server that runs on your Linux VPS. It receives relay requests forwarded by Google Apps Script on behalf of EasyRelayGoogle and makes the actual outbound HTTP/HTTPS connections using your VPS's IP address.

```
Browser → Local Proxy → Apps Script (Google) → VPS Exit Node → Target Website
```

---

## Table of Contents

- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Python Server](#python-server)
- [Dockerfile](#dockerfile)
- [Docker Compose](#docker-compose)
- [API Reference](#api-reference)
- [Managing the Service](#managing-the-service)
- [Updating](#updating)

---

## How It Works

The exit node acts as the final hop in the relay chain. Google Apps Script cannot make arbitrary outbound requests on its own, so it forwards the relay payload to this server. The server then:

1. Validates the pre-shared key (PSK) in the request body
2. Checks the target URL is safe (blocks SSRF via private/loopback ranges)
3. Strips proxy and hop-by-hop headers
4. Opens an HTTP/HTTPS connection to the target using a persistent connection pool
5. Returns the response (status, headers, base64-encoded body) as JSON

---

## Requirements

- A Linux VPS (Ubuntu 22.04+ recommended)
- Docker Engine 24+
- Docker Compose v2+

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/your-user/your-repo.git /opt/exit-node
cd /opt/exit-node

# 2. Create environment file
cp .env.example .env
nano .env          # set EXIT_NODE_PSK at minimum

# 3. Build and start
docker compose up -d

# 4. Verify
curl http://localhost:8181/
# → {"ok": true, "status": "healthy", ...}
```

---

## Configuration

All configuration is done through environment variables, either in the `.env` file or passed directly to Docker.

### `.env` file

```env
# ── Required ──────────────────────────────────────────────
EXIT_NODE_PSK=your_strong_secret_here   # pre-shared key, must match client config

# ── Network ───────────────────────────────────────────────
EXIT_NODE_PORT=8181                     # TCP port to listen on
EXIT_NODE_HOST=0.0.0.0                  # bind address

# ── Performance ───────────────────────────────────────────
EXIT_NODE_WORKERS=50                    # thread pool size
EXIT_NODE_CONNECT_TIMEOUT=5             # TCP connect timeout in seconds
EXIT_NODE_READ_TIMEOUT=25               # response read timeout in seconds
EXIT_NODE_POOL_SIZE=20                  # max kept-alive connections per host

# ── Limits ────────────────────────────────────────────────
EXIT_NODE_MAX_REQUEST_MB=32             # max inbound request body (MiB)
EXIT_NODE_MAX_RESPONSE_MB=64            # max outbound response body (MiB)

# ── Logging ───────────────────────────────────────────────
EXIT_NODE_LOG_LEVEL=INFO                # DEBUG / INFO / WARNING / ERROR
```

> **Security:** Never commit `.env` to version control. Add it to `.gitignore`.

### Variable reference

| Variable | Default | Description |
|---|---|---|
| `EXIT_NODE_PSK` | — | **Required.** Pre-shared key for request authentication. |
| `EXIT_NODE_PORT` | `8181` | TCP port the server listens on. |
| `EXIT_NODE_HOST` | `0.0.0.0` | Bind address. Use `0.0.0.0` to accept all interfaces. |
| `EXIT_NODE_WORKERS` | `50` | Number of threads in the pool. Increase for high concurrency. |
| `EXIT_NODE_CONNECT_TIMEOUT` | `5` | Seconds to wait for TCP connection to target. |
| `EXIT_NODE_READ_TIMEOUT` | `25` | Seconds to wait for response body from target. |
| `EXIT_NODE_POOL_SIZE` | `20` | Max idle keep-alive connections stored per host. |
| `EXIT_NODE_MAX_REQUEST_MB` | `32` | Max accepted request body size in MiB. |
| `EXIT_NODE_MAX_RESPONSE_MB` | `64` | Max forwarded response body size in MiB. |
| `EXIT_NODE_LOG_LEVEL` | `INFO` | Logging verbosity. |

---

## Python Server

**File:** `vps_exit_node.py`

### Architecture

```
_ThreadPoolServer (TCPServer + ThreadPoolExecutor)
    └── _ExitNodeHandler (BaseHTTPRequestHandler)
            ├── GET  /   →  health check
            └── POST /   →  relay request
                    └── _ConnectionPool.request()
                            └── http.client.HTTP(S)Connection
```

### Key components

#### `_ConnectionPool`

A thread-safe HTTP/HTTPS keep-alive connection pool. Maintains a `queue.Queue` per `(scheme, host, port)` tuple.

- On request: tries to grab an idle connection from the queue. If none available, opens a new one.
- On response: returns the connection to the queue if the socket is still alive, otherwise closes it.
- On failure: closes the stale connection, creates a fresh one, and retries the request once.
- Uses a **separate connect timeout** (via `socket.create_connection`) and a **read timeout** (via `http.client` timeout), which was not possible with the original `urllib.request`.

#### `_ThreadPoolServer`

Replaces `ThreadingMixIn` with a `ThreadPoolExecutor`. Threads are pre-created at startup, eliminating per-request thread creation overhead.

Applies TCP socket tuning at startup:

| Option | Value | Effect |
|---|---|---|
| `TCP_NODELAY` | 1 | Disables Nagle — reduces latency for small responses |
| `SO_KEEPALIVE` | 1 | Detects dead inbound connections |
| `SO_RCVBUF` / `SO_SNDBUF` | 262144 | 256 KiB buffers — fewer syscalls at high throughput |
| `TCP_KEEPIDLE` | 30s | Start probing after 30s idle (Linux only) |
| `TCP_KEEPINTVL` | 10s | Probe interval (Linux only) |
| `TCP_KEEPCNT` | 3 | Drop after 3 failed probes (Linux only) |

#### `_sanitize_headers`

Strips all hop-by-hop and proxy headers before forwarding to the target. Also removes `Accept-Encoding` because `http.client` cannot decompress gzip/br/deflate responses.

#### `_safe_url`

Blocks SSRF by rejecting requests to loopback addresses (`127.x.x.x`, `::1`), private ranges (`10.x`, `172.16–31.x`, `192.168.x`), link-local (`169.254.x`), and ULA IPv6 (`fc00::/7`).

#### `_collect_headers`

Preserves duplicate response header names (e.g. multiple `Set-Cookie` lines) by accumulating them into a list, preventing silent value loss.

### Running without Docker

```bash
export EXIT_NODE_PSK=your_secret
python3 vps_exit_node.py --port 8181 --log-level INFO
```

Or with all options as flags:

```bash
python3 vps_exit_node.py \
  --psk your_secret \
  --host 0.0.0.0 \
  --port 8181 \
  --log-level DEBUG
```

---

## Dockerfile

**File:** `Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY vps_exit_node.py .

# Non-root user for security
RUN useradd -r -s /bin/false exitnode
USER exitnode

EXPOSE 8181

CMD ["sh", "-c", "python vps_exit_node.py --port ${EXIT_NODE_PORT:-8181}"]
```

### Design decisions

- **`python:3.12-slim`** — minimal image, no unnecessary packages. The server uses only the Python standard library so no `pip install` step is needed.
- **Non-root user** — the process runs as `exitnode` (UID allocated by `useradd -r`). If the process is compromised, it cannot write to the filesystem or escalate privileges.
- **Port via ENV** — `EXPOSE` is documentation only; the actual port is read from `EXIT_NODE_PORT` at runtime so you can change it without rebuilding.

---

## Docker Compose

**File:** `docker-compose.yml`

```yaml
services:
  exit-node:
    build: .
    restart: always
    ports:
      - "${EXIT_NODE_PORT:-8181}:${EXIT_NODE_PORT:-8181}"
    env_file:
      - .env
    environment:
      - EXIT_NODE_PSK=${EXIT_NODE_PSK}
      - EXIT_NODE_PORT=${EXIT_NODE_PORT:-8181}
      - EXIT_NODE_HOST=${EXIT_NODE_HOST:-0.0.0.0}
      - EXIT_NODE_WORKERS=${EXIT_NODE_WORKERS:-50}
      - EXIT_NODE_CONNECT_TIMEOUT=${EXIT_NODE_CONNECT_TIMEOUT:-5}
      - EXIT_NODE_READ_TIMEOUT=${EXIT_NODE_READ_TIMEOUT:-25}
      - EXIT_NODE_POOL_SIZE=${EXIT_NODE_POOL_SIZE:-20}
      - EXIT_NODE_MAX_REQUEST_MB=${EXIT_NODE_MAX_REQUEST_MB:-32}
      - EXIT_NODE_MAX_RESPONSE_MB=${EXIT_NODE_MAX_RESPONSE_MB:-64}
      - EXIT_NODE_LOG_LEVEL=${EXIT_NODE_LOG_LEVEL:-INFO}
    healthcheck:
      test:
        [
          "CMD", "python", "-c",
          "import urllib.request; urllib.request.urlopen('http://localhost:${EXIT_NODE_PORT:-8181}/')",
        ]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### Key settings explained

**`restart: always`**
Docker automatically restarts the container if it crashes or if the host reboots. Equivalent to the `Restart=always` in the original systemd unit.

**`env_file` + `environment`**
`env_file` loads all variables from `.env`. The `environment` block then explicitly maps each one into the container with a fallback default. This means the container starts correctly even if a variable is missing from `.env`.

**`healthcheck`**
Docker queries `GET /` every 30 seconds. If the server does not respond with HTTP 200 within 5 seconds for 3 consecutive checks, the container is marked `unhealthy`. `start_period: 10s` gives the server time to initialize before checks begin.

**`logging`**
Limits log files to 10 MiB each with a maximum of 3 rotated files (30 MiB total on disk). Prevents the container from filling the VPS disk over time.

---

## API Reference

### `GET /`

Health check. No authentication required.

**Response `200`**
```json
{
  "ok": true,
  "status": "healthy",
  "message": "VPS exit node is running.",
  "workers": 50,
  "pool": 20
}
```

---

### `POST /`

Relay a request to a target URL.

**Request body**
```json
{
  "k": "your_psk",
  "u": "https://target-site.com/api/endpoint",
  "m": "POST",
  "h": {
    "Authorization": "Bearer token",
    "Content-Type": "application/json"
  },
  "b": "<base64-encoded request body>"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `k` | string | yes | Pre-shared key |
| `u` | string | yes | Target URL (must be public http/https) |
| `m` | string | no | HTTP method, default `GET` |
| `h` | object | no | Request headers to forward |
| `b` | string | no | Base64-encoded request body |

**Response `200`**
```json
{
  "s": 200,
  "h": {
    "Content-Type": "application/json",
    "Set-Cookie": ["session=abc", "csrf=xyz"]
  },
  "b": "<base64-encoded response body>"
}
```

**Error responses**

| Status | `e` field | Cause |
|---|---|---|
| `400` | `empty_body` | No request body |
| `400` | `bad_json` | Body is not valid JSON |
| `400` | `bad_url` | URL is missing, malformed, or targets a private address |
| `400` | `bad_base64` | `b` field is not valid base64 |
| `401` | `unauthorized` | PSK mismatch |
| `413` | `request_too_large` | Body exceeds `EXIT_NODE_MAX_REQUEST_MB` |
| `500` | `server_psk_missing` | Server started without a PSK |
| `500` | *(exception message)* | Outbound request failed |

---

## Managing the Service

```bash
# Start in background
docker compose up -d

# View live logs
docker compose logs -f

# Check health status
docker compose ps

# Stop
docker compose down

# Restart
docker compose restart
```

### Add to client `config.json`

After deployment, add this block to your client configuration:

```json
"exit_node": {
  "enabled": true,
  "provider": "vps",
  "url": "http://YOUR-VPS-IP:8181",
  "psk": "your_psk_here",
  "mode": "full",
  "hosts": []
}
```

---

## Updating

```bash
cd /opt/exit-node

# Pull latest code
git pull

# Rebuild image and restart container
docker compose up -d --build
```

The old container keeps serving traffic until the new one is ready.