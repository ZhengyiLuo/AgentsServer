#!/usr/bin/env python3
"""Fixture-only HTTPS tarball replay on disposable GitHub-hosted native runners.

This is transport substitution, not public-registry acceptance. The caller must
verify the signed npm descriptor and pass its exact URL, SHA-256 and byte size.
No product code, public release, system trust store or npm registry is changed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import ssl
import stat
import subprocess
import sys
import threading
from urllib.parse import urlsplit

HOST = "registry.npmjs.org"
MARKER = "AGENTSDOCK-NATIVE-NPM-REPLAY"
HOSTS = Path("/etc/hosts")
LOCK = Path("/tmp/agentsdock-native-npm-replay.lock")
MAX_ARCHIVE = 200 * 1024 * 1024
CI_KEYS = ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "GITHUB_REPOSITORY", "GITHUB_EVENT_NAME",
           "GITHUB_WORKFLOW_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_TEMP",
           "AGENTSDOCK_NATIVE_UPGRADE_ACCEPTANCE")


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def guard(work: Path, *, child=False):
    env = os.environ
    need(env.get("GITHUB_ACTIONS") == "true" and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
         and env.get("GITHUB_REPOSITORY") == "ZhengyiLuo/AgentsServer"
         and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
         and env.get("AGENTSDOCK_NATIVE_UPGRADE_ACCEPTANCE") == "true",
         "HTTPS replay is restricted to explicitly dispatched disposable hosted acceptance.")
    need(re.fullmatch(r"ZhengyiLuo/AgentsServer/\.github/workflows/(?:server-release|server-native-acceptance)\.yml@refs/heads/(?:main|release/[A-Za-z0-9][A-Za-z0-9._/-]*)",
                      env.get("GITHUB_WORKFLOW_REF", "")), "Unexpected replay workflow.")
    need(all(re.fullmatch(r"[1-9][0-9]*", env.get(key, "")) for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")),
         "Missing replay run identity.")
    need((platform.system(), env.get("RUNNER_OS")) in {("Darwin", "macOS"), ("Linux", "Linux")},
         "Unexpected replay platform.")
    need((os.geteuid() == 0) == child, "Only the tracked listener child may run as root.")
    owner = int(env.get("SUDO_UID", "0")) if child else os.getuid()
    need(owner > 0, "Replay requires a non-root disposable runner account.")
    root = Path(env.get("RUNNER_TEMP", "")).resolve()
    need(root.is_dir() and root != Path("/") and work.is_absolute() and work == work.resolve()
         and work.is_relative_to(root) and work != root, "Replay work must be an unlinked RUNNER_TEMP child.")
    return root, owner


def regular(path: Path, limit=1024 * 1024):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "Invalid replay input.")
        data = stream.read(limit + 1)
        need(len(data) <= limit, "Replay input exceeds its bound.")
        return data


def write_private(path: Path, data: bytes):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def command(*args):
    result = subprocess.run(args, capture_output=True, timeout=30,
                            env={key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ})
    need(result.returncode == 0, f"Fixture command failed: {Path(args[0]).name}; output withheld.")
    return result.stdout


def archive_path(signed_url: str):
    url = urlsplit(signed_url)
    need(url.scheme == "https" and url.netloc == HOST and not url.query and not url.fragment
         and re.fullmatch(r"/@agentsdock/server/-/server-\d+\.\d+\.\d+-beta\.[1-9][0-9]*\.tgz", url.path),
         "Replay requires the unchanged signed npm beta tarball URL.")
    return url.path


def overlay(baseline: bytes, marker: str):
    need(MARKER.encode() not in baseline, "Another replay hosts overlay exists.")
    for line in baseline.decode().splitlines():
        need(HOST not in [item.lower().rstrip(".") for item in line.split("#", 1)[0].split()[1:]],
             "Registry already has a hosts override.")
    block = f"# BEGIN {marker}\n127.0.0.1 {HOST}\n# END {marker}\n".encode()
    return baseline + (b"" if not baseline or baseline.endswith(b"\n") else b"\n") + block, block


def replace_hosts(expected: bytes, replacement: bytes):
    with os.fdopen(os.open(HOSTS, os.O_RDWR | os.O_NOFOLLOW), "r+b") as stream:
        need(stream.read() == expected, "Hosts changed outside the owned replay operation.")
        stream.seek(0)
        stream.write(replacement)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    if platform.system() == "Darwin":
        command("/usr/bin/dscacheutil", "-flushcache")
        command("/usr/bin/killall", "-HUP", "mDNSResponder")


def public_ca_roots():
    context = ssl.create_default_context()
    roots = context.get_ca_certs(binary_form=True)
    if not roots:
        # Standalone Python builds may use a lazy capath without an eager
        # cafile. Export the runner's existing OS roots into our local bundle.
        bundle = {"Linux": "/etc/ssl/certs/ca-certificates.crt", "Darwin": "/etc/ssl/cert.pem"}.get(platform.system())
        if bundle and Path(bundle).is_file():
            context.load_verify_locations(cafile=bundle)
            roots = context.get_ca_certs(binary_form=True)
    need(roots, "Default public TLS roots are required alongside the fixture CA.")
    return roots


def certificates(work: Path):
    write_private(work / "ca.cnf", b"[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n[dn]\nCN=AgentsDock fixture CA\n[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n")
    write_private(work / "leaf.cnf", b"[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=registry.npmjs.org\n")
    write_private(work / "leaf.ext", b"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:registry.npmjs.org\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
    previous = os.umask(0o077)
    try:
        command("openssl", "req", "-config", str(work / "ca.cnf"), "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "1", "-keyout", str(work / "ca.key"), "-out", str(work / "ca.pem"))
        command("openssl", "req", "-config", str(work / "leaf.cnf"), "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-keyout", str(work / "leaf.key"), "-out", str(work / "leaf.csr"))
        command("openssl", "x509", "-req", "-sha256", "-days", "1", "-in", str(work / "leaf.csr"), "-CA", str(work / "ca.pem"), "-CAkey", str(work / "ca.key"), "-CAcreateserial", "-extfile", str(work / "leaf.ext"), "-out", str(work / "leaf.pem"))
    finally:
        os.umask(previous)
    command("openssl", "verify", "-CAfile", str(work / "ca.pem"), "-purpose", "sslserver", "-verify_hostname", HOST, str(work / "leaf.pem"))
    roots = public_ca_roots()
    write_private(work / "trust-bundle.pem", "".join(ssl.DER_cert_to_PEM_cert(cert) for cert in roots).encode() + regular(work / "ca.pem"))


def handler_for(payload: bytes, path: str, receipt: dict):
    class ExactArchiveHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def serve(self, body):
            hosts = self.headers.get_all("Host", [])
            valid = hosts in ([HOST], [HOST + ":443"]) and getattr(self.connection, "replay_sni", None) == HOST
            valid = valid and self.path == path and not any(self.headers.get_all(name) for name in
                ("Authorization", "Proxy-Authorization", "Cookie", "Range", "Transfer-Encoding", "Content-Length"))
            if not valid:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-AgentsDock-Replay", "fixture-only-not-public-registry")
            self.end_headers()
            if body:
                self.wfile.write(payload)
                self.wfile.flush()
                receipt["successful_gets"] += 1
                receipt["successful_bytes"] += len(payload)

        def do_GET(self):
            self.serve(True)

        def do_HEAD(self):
            self.serve(False)
    return ExactArchiveHandler


def serve(work: Path):
    root, owner = guard(work, child=True)
    need(work.stat().st_uid == owner and stat.S_IMODE(work.stat().st_mode) == 0o700, "Wrong replay owner/mode.")
    config = json.loads(regular(work / "input.json"))
    path = archive_path(config["url"])
    archive = Path(config["archive"])
    need(archive == archive.resolve() and archive.is_relative_to(root), "Archive is outside runner inputs.")
    payload = regular(archive, MAX_ARCHIVE)
    need(len(payload) == config["size"] and hashlib.sha256(payload).hexdigest() == config["sha256"], "Archive differs from signed descriptor.")
    need(stat.S_IMODE((work / "leaf.key").stat().st_mode) == 0o600, "Fixture TLS key is not private.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(work / "leaf.pem", work / "leaf.key")
    def sni(sock, name, _context):
        if name != HOST:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        sock.replay_sni = name
    context.set_servername_callback(sni)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threading.Thread(target=lambda: (sys.stdin.buffer.read(), stop.set()), daemon=True).start()
    receipt = {"successful_gets": 0, "successful_bytes": 0, "hosts_restored": False}
    server = None
    baseline = active = block = None
    LOCK.mkdir(mode=0o700)
    try:
        baseline = regular(HOSTS)
        marker = f"{MARKER} {os.environ['GITHUB_RUN_ID']}/{os.environ['GITHUB_RUN_ATTEMPT']}"
        active, block = overlay(baseline, marker)
        class BoundedTLSServer(ThreadingHTTPServer):
            def get_request(self):
                connection, address = self.socket.accept()
                connection.settimeout(5)
                try:
                    return context.wrap_socket(connection, server_side=True), address
                except BaseException:
                    connection.close()
                    raise
        server = BoundedTLSServer(("127.0.0.1", 443), handler_for(payload, path, receipt))
        server.daemon_threads = True
        server.timeout = 0.2
        write_private(work / "hosts.baseline", baseline)
        replace_hosts(baseline, active)
        print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
        while not stop.is_set():
            server.handle_request()
    finally:
        if server is not None:
            server.server_close()
        if baseline is not None and active is not None:
            current = regular(HOSTS)
            if current == active:
                replace_hosts(active, baseline)
            elif current != baseline:
                need(block is not None and current.count(block) == 1, "Hosts ownership changed during replay cleanup.")
                replace_hosts(current, current.replace(block, b""))
            receipt["hosts_restored"] = regular(HOSTS) == baseline
        LOCK.rmdir()
        print(json.dumps({"cleanup": receipt}), flush=True)


@dataclass
class Replay:
    environment: dict
    receipt: dict


@contextmanager
def replay_signed_npm_archive(archive: Path, signed_url: str, work: Path, *, expected_sha256: str, expected_size: int):
    root, _owner = guard(work)
    archive_path(signed_url)
    need(archive.is_absolute() and archive == archive.resolve() and archive.is_relative_to(root), "Archive must be a runner input.")
    need(re.fullmatch(r"[a-f0-9]{64}", expected_sha256) and type(expected_size) is int and 0 < expected_size <= MAX_ARCHIVE,
         "Exact signed archive hash and size are required.")
    need(not work.exists(), "Replay work directory must be fresh.")
    work.mkdir(mode=0o700)
    process = None
    replay = Replay({}, {"used": True, "transport": "loopback-https-replay", "public_registry_e2e": False,
        "url": signed_url, "sha256": expected_sha256, "size": expected_size,
        "successful_gets": 0, "successful_bytes": 0, "hosts_restored": False})
    try:
        certificates(work)
        write_private(work / "input.json", json.dumps({"archive": str(archive), "url": signed_url, "sha256": expected_sha256, "size": expected_size}).encode())
        bundle = str(work / "trust-bundle.pem")
        no_proxy = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), HOST, "localhost", "127.0.0.1", "::1"]))
        replay.environment = {"SSL_CERT_FILE": bundle, "REQUESTS_CA_BUNDLE": bundle, "CURL_CA_BUNDLE": bundle,
            "NODE_EXTRA_CA_CERTS": str(work / "ca.pem"), "NO_PROXY": no_proxy, "no_proxy": no_proxy}
        command_env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        arguments = ["sudo", "-n", "/usr/bin/env", *(f"{key}={os.environ[key]}" for key in CI_KEYS if key in os.environ),
            sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--serve", str(work)]
        with (work / "listener.stderr").open("wb") as errors:
            process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, env=command_env)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            need(selector.select(timeout=30), "Replay listener did not become ready.")
        ready = json.loads(process.stdout.readline())
        need(ready.get("ready") is True and type(ready.get("pid")) is int, "Replay listener failed to initialize.")
        yield replay
    finally:
        try:
            if process is not None:
                process.stdin.close()
                process.stdin = None
                try:
                    output, _ = process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    output, _ = process.communicate(timeout=15)
                rows = [json.loads(line) for line in output.splitlines() if line.startswith(b"{")]
                cleanup = next((row["cleanup"] for row in rows if "cleanup" in row), {})
                replay.receipt.update(cleanup)
                need(process.returncode == 0 and cleanup.get("hosts_restored") is True,
                     "Replay child or hosts cleanup failed; see owned fixture diagnostics.")
        finally:
            for name in ("ca.key", "leaf.key", "leaf.csr", "ca.srl"):
                path = work / name
                if path.exists():
                    need(path.is_file() and not path.is_symlink(), "Unexpected fixture key cleanup target.")
                    path.unlink()
            replay.receipt["fixture_keys_removed"] = True



if __name__ == "__main__":
    need(len(sys.argv) == 3 and sys.argv[1] == "--serve", "Internal fixture listener only.")
    serve(Path(sys.argv[2]))
