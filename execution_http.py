"""Credential HTTP transport pinned to the connected native process.

The Linux /proc and Darwin lsof tuple checks are shared in design with
install.sh:pinned_managed_http_get. No HTTP bytes, including credentials, leave
the connected socket before process ownership and caller authority are proven.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Callable
from urllib.parse import urlsplit

MAXIMUM_HEADERS = 64 * 1024


def address_from_proc(raw, ipv6):
    packed = bytes.fromhex(raw)
    if not ipv6:
        packed = packed[::-1]
    else:
        packed = b"".join(
            packed[offset : offset + 4][::-1]
            for offset in range(0, 16, 4)
        )
    return ipaddress.ip_address(packed)


def linux_connection_owned(pid, server, client):
    inodes = set()
    try:
        entries = (Path("/proc") / str(pid) / "fd").iterdir()
        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    except OSError:
        return False
    if not inodes:
        return False
    expected_server = (ipaddress.ip_address(server[0]), int(server[1]))
    expected_client = (ipaddress.ip_address(client[0]), int(client[1]))
    for table, ipv6 in ((Path("/proc/net/tcp"), False), (Path("/proc/net/tcp6"), True)):
        try:
            lines = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "01" or fields[9] not in inodes:
                continue
            try:
                local_address, local_port = fields[1].rsplit(":", 1)
                remote_address, remote_port = fields[2].rsplit(":", 1)
                observed_server = (
                    address_from_proc(local_address, ipv6),
                    int(local_port, 16),
                )
                observed_client = (
                    address_from_proc(remote_address, ipv6),
                    int(remote_port, 16),
                )
            except (IndexError, ValueError):
                continue
            if observed_server == expected_server and observed_client == expected_client:
                return True
    return False


def parse_lsof_endpoint(value):
    value = value.strip().split(None, 1)[0]
    if value.startswith("["):
        closing = value.find("]:")
        if closing < 0:
            raise ValueError
        host = value[1:closing]
        port = value[closing + 2 :]
    else:
        host, port = value.rsplit(":", 1)
    return ipaddress.ip_address(host), int(port)


def darwin_connection_owned(pid, server, client, timeout):
    try:
        result = subprocess.run(
            [
                "/usr/sbin/lsof",
                "-nP",
                "-a",
                "-p",
                str(pid),
                "-iTCP",
                "-sTCP:ESTABLISHED",
                "-Fn",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(0.05, timeout),
            check=False,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    expected_server = (ipaddress.ip_address(server[0]), int(server[1]))
    expected_client = (ipaddress.ip_address(client[0]), int(client[1]))
    for raw_line in result.stdout.decode("ascii", "strict").splitlines():
        if not raw_line.startswith("n") or "->" not in raw_line:
            continue
        try:
            local, remote = raw_line[1:].split("->", 1)
            if (
                parse_lsof_endpoint(local) == expected_server
                and parse_lsof_endpoint(remote) == expected_client
            ):
                return True
        except (UnicodeError, ValueError):
            continue
    return False


def receive_response(connection, deadline, maximum_body):
    payload = bytearray()
    header_end = -1
    while header_end < 0:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("pinned HTTP response is invalid")
        connection.settimeout(remaining)
        chunk = connection.recv(min(64 * 1024, MAXIMUM_HEADERS + 4 - len(payload)))
        if not chunk:
            raise RuntimeError("pinned HTTP response is invalid")
        payload.extend(chunk)
        header_end = payload.find(b"\r\n\r\n")
        if header_end > MAXIMUM_HEADERS or (header_end < 0 and len(payload) > MAXIMUM_HEADERS):
            raise RuntimeError("pinned HTTP response is invalid")
    raw_headers = bytes(payload[:header_end])
    body = bytearray(payload[header_end + 4 :])
    try:
        lines = raw_headers.decode("latin-1").split("\r\n")
    except UnicodeError:
        raise RuntimeError("pinned HTTP response is invalid")
    status_parts = lines[0].split(" ", 2) if lines else []
    if (
        len(status_parts) < 2
        or status_parts[0] not in ("HTTP/1.0", "HTTP/1.1")
        or status_parts[1] != "200"
    ):
        raise RuntimeError("pinned HTTP response is invalid")
    headers = {}
    for line in lines[1:]:
        if not line or line[:1] in " \t" or ":" not in line:
            raise RuntimeError("pinned HTTP response is invalid")
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if not name or name in headers:
            raise RuntimeError("pinned HTTP response is invalid")
        headers[name] = value
    if "transfer-encoding" in headers:
        raise RuntimeError("pinned HTTP response is invalid")
    content_length = None
    if "content-length" in headers:
        try:
            content_length = int(headers["content-length"], 10)
        except ValueError:
            raise RuntimeError("pinned HTTP response is invalid")
        if content_length < 0 or content_length > maximum_body:
            raise RuntimeError("pinned HTTP response is invalid")
    if len(body) > maximum_body or (
        content_length is not None and len(body) > content_length
    ):
        raise RuntimeError("pinned HTTP response is invalid")
    while content_length is None or len(body) < content_length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("pinned HTTP response is invalid")
        connection.settimeout(remaining)
        chunk = connection.recv(min(64 * 1024, maximum_body + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > maximum_body:
            raise RuntimeError("pinned HTTP response is invalid")
    if content_length is not None and len(body) != content_length:
        raise RuntimeError("pinned HTTP response is invalid")
    return bytes(body)


def request_json(url: str, token: str, *, expected_pid: int, platform: str,
                 verify_owner: Callable[[], None], body: dict | None = None,
                 timeout: float = 5.0, maximum_body: int = 1024 * 1024) -> dict:
    """Connect once, prove the exact peer PID, then send one GET or POST."""
    if type(expected_pid) is not int or expected_pid <= 1 or platform not in {"Linux", "Darwin"}:
        raise ValueError("pinned HTTP process identity is invalid")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or not parsed.hostname or not parsed.port
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("pinned HTTP requires an exact local IP endpoint")
    # Numeric addresses avoid DNS or proxy rewriting the admitted endpoint.
    ipaddress.ip_address(parsed.hostname)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    if any(ord(char) < 33 or ord(char) > 126 for char in path):
        raise ValueError("pinned HTTP path is invalid")
    if (not isinstance(token, str) or not 1 <= len(token) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise ValueError("pinned HTTP credential format is invalid")
    if not 0 < timeout <= 60 or not 0 < maximum_body <= 16 * 1024 * 1024:
        raise ValueError("pinned HTTP bounds are invalid")
    payload = b"" if body is None else json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > maximum_body:
        raise ValueError("pinned HTTP request exceeds limit")
    deadline = time.monotonic() + timeout
    with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout) as connection:
        server, client = connection.getpeername()[:2], connection.getsockname()[:2]
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("pinned HTTP connection ownership could not be proven")
            owned = (linux_connection_owned(expected_pid, server, client) if platform == "Linux" else
                     darwin_connection_owned(expected_pid, server, client, remaining))
            if owned:
                break
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        # A valid socket belonging to a stale epoch is still not the admitted
        # native role. Recheck its service manager and owned receipt now.
        verify_owner()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("pinned HTTP ownership verification timed out")
        host = parsed.hostname
        host_header = f"[{host}]:{parsed.port}" if ":" in host else f"{host}:{parsed.port}"
        headers = (("POST" if body is not None else "GET") + " " + path + " HTTP/1.1\r\n"
                   + "Host: " + host_header + "\r\nAuthorization: Bearer " + token
                   + "\r\nAccept: application/json\r\nConnection: close\r\n")
        if body is not None:
            headers += "Content-Type: application/json\r\nContent-Length: " + str(len(payload)) + "\r\n"
        connection.settimeout(remaining)
        connection.sendall(headers.encode("ascii") + b"\r\n" + payload)
        response = receive_response(connection, deadline, maximum_body)
    result = json.loads(response)
    if not isinstance(result, dict):
        raise RuntimeError("pinned HTTP returned an invalid object")
    return result
