"""No privileged mutation: verify fixture guards, TLS and exact archive routing."""
import hashlib
from http.client import HTTPResponse
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import native_release_replay as replay


class NativeReleaseReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temporary.name)
        replay.certificates(cls.work)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_guard_rejects_developer_host_before_any_host_change(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'disposable hosted'):
                replay.guard(self.work)

    def test_only_unchanged_signed_npm_origin_and_exact_beta_path_are_allowed(self):
        path = '/@agentsdock/server/-/server-1.0.7-beta.21.tgz'
        self.assertEqual(replay.archive_path('https://registry.npmjs.org' + path), path)
        for url in ('http://registry.npmjs.org' + path, 'https://localhost' + path,
                    'https://registry.npmjs.org:443' + path, 'https://registry.npmjs.org' + path + '?other=1',
                    'https://registry.npmjs.org/@other/package.tgz'):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                replay.archive_path(url)

    def test_hosts_overlay_preserves_original_bytes_and_rejects_existing_registry_mapping(self):
        baseline = b'127.0.0.1 localhost\n::1 localhost\n'
        active, block = replay.overlay(baseline, replay.MARKER + ' 1/1')
        self.assertEqual(active.removesuffix(block), baseline)
        self.assertIn(b'127.0.0.1 registry.npmjs.org\n', block)
        with self.assertRaises(RuntimeError):
            replay.overlay(baseline + b'10.0.0.1 REGISTRY.NPMJS.ORG.\n', 'fixture')
        with self.assertRaises(RuntimeError):
            replay.overlay(active, 'fixture')

    def test_trust_bundle_retains_public_roots_and_adds_fixture_root(self):
        ca = (self.work / 'ca.pem').read_bytes()
        bundle = (self.work / 'trust-bundle.pem').read_bytes()
        self.assertTrue(bundle.endswith(ca))
        self.assertGreater(bundle.count(b'BEGIN CERTIFICATE'), 1)
        self.assertEqual((self.work / 'leaf.key').stat().st_mode & 0o777, 0o600)

    def test_empty_default_trust_store_loads_existing_os_roots_without_disabling_verification(self):
        # Reproduce uv Python's empty enumeration using a real empty store.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(context.get_ca_certs(binary_form=True), [])
        with patch.object(replay.ssl, 'create_default_context', return_value=context):
            roots = replay.public_ca_roots()
        self.assertGreater(len(roots), 1)
        self.assertEqual(roots, context.get_ca_certs(binary_form=True))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_missing_public_roots_still_fails_instead_of_trusting_only_fixture(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(replay.ssl, 'create_default_context', return_value=context), \
                patch.object(replay.Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'Default public TLS roots'):
                replay.public_ca_roots()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_https_responds_only_to_exact_origin_route_and_never_counts_head_as_download(self):
        payload = b'exact signed fixture archive bytes' * 128
        path = '/@agentsdock/server/-/server-1.0.7-beta.21.tgz'
        receipt = {'successful_gets': 0, 'successful_bytes': 0}
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.work / 'leaf.pem', self.work / 'leaf.key')
        def sni(connection, name, _context):
            if name != replay.HOST:
                return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
            connection.replay_sni = name
        context.set_servername_callback(sni)
        server = ThreadingHTTPServer(('127.0.0.1', 0), replay.handler_for(payload, path, receipt))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        client = ssl.create_default_context(cafile=str(self.work / 'trust-bundle.pem'))
        def request(method='GET', target=path, host=replay.HOST, extra=''):
            with socket.create_connection(server.server_address, timeout=5) as raw:
                with client.wrap_socket(raw, server_hostname=replay.HOST) as connection:
                    connection.sendall(f'{method} {target} HTTP/1.1\r\nHost: {host}\r\n{extra}\r\n'.encode())
                    response = HTTPResponse(connection, method=method)
                    response.begin()
                    return response.status, response.read()
        try:
            self.assertEqual(request('HEAD'), (200, b''))
            self.assertEqual(receipt['successful_gets'], 0)
            status, actual = request()
            self.assertEqual(status, 200)
            self.assertEqual(hashlib.sha256(actual).digest(), hashlib.sha256(payload).digest())
            self.assertEqual(receipt, {'successful_gets': 1, 'successful_bytes': len(payload)})
            for kwargs in ({'target': '/'}, {'target': path + '?x=1'}, {'host': 'other.invalid'},
                           {'extra': 'Authorization: secret\r\n'}, {'extra': 'Cookie: secret\r\n'},
                           {'extra': 'Range: bytes=0-1\r\n'}, {'extra': 'Host: other.invalid\r\n'},
                           {'method': 'POST'}):
                with self.subTest(kwargs=kwargs):
                    self.assertGreaterEqual(request(**kwargs)[0], 400)
            self.assertEqual(receipt['successful_gets'], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
