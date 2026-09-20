"""Exercise the installer's real candidate health validator without a service."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).with_name('install.sh').read_text()
HEALTH_PYTHON = SOURCE.split('release_health_check_once() {', 1)[1].split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]


class CoordinatedInstallerHealthTests(unittest.TestCase):
    def check_health(self, api, expected='28'):
        health = {
            'ok': True, 'server_version': '1.0.4-beta.12',
            'server_identity': 'server-test-identity',
            'capabilities': {
                'team_hub_v1': {
                    'available': False, 'designated_host': False, 'version': 1,
                    'base_path': None, 'hub_id': None, 'host_server_identity': None,
                    'transport': None, 'hub_url': None, 'routes': [],
                },
                'secure_peer_v1': {
                    'available': True, 'state_available': True, 'state_error_code': None,
                    'required': False, 'version': 1,
                    'control_path': '/api/admin/secure-peers/v1/status',
                    'proxy_prefix': '/api/team-hub-secure',
                },
            },
        }
        if api is not None:
            health['api_contract_version'] = api
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / 'health.json'
            fixture.write_text(json.dumps(health))
            return subprocess.run(
                [sys.executable, '-', str(fixture), '1.0.4-beta.12', 'server-test-identity',
                 'disabled', '', 'loopback', '', '', 'false', '', expected],
                input=HEALTH_PYTHON, text=True, capture_output=True,
            )

    def test_exact_signed_api_is_required_before_candidate_commit(self):
        result = self.check_health(28)
        self.assertEqual(result.returncode, 0, result.stderr)
        for api in (27, 29, '28', True, None):
            with self.subTest(api=api):
                result = self.check_health(api)
                self.assertEqual(result.returncode, 1, result.stderr)

    def test_legacy_health_without_a_signed_api_pin_stays_compatible(self):
        result = self.check_health(None, expected='')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_candidate_check_forwards_pin_but_recovery_of_another_version_does_not(self):
        function = SOURCE.split('wait_for_release_health() {', 1)[1].split('\nsecure_peer_host_attachment_check_once()', 1)[0]
        for requested, expected in [('1.0.4-beta.12', '28'), ('1.0.4-beta.13', '')]:
            with self.subTest(requested=requested):
                script = '\n'.join([
                    'set -eu',
                    'RELEASE_VERSION=1.0.4-beta.12', f'REQUESTED_RELEASE_VERSION={requested}',
                    'EXPECTED_API_CONTRACT=28', 'RELEASE_DIR=/disposable/candidate',
                    'EXPECTED_SERVER_IDENTITY=server-test-identity', 'TEAM_HUB_MODE=disabled',
                    'EXPECTED_TEAM_HUB_ID=', 'TEAM_HUB_REACTIVATION_HUB_ID=',
                    'TEAM_HUB_TRANSPORT=loopback', 'TEAM_HUB_URL=',
                    'HEALTH_CHECK_ATTEMPTS=1', 'PORT=17850', 'BIND_ADDRESS=127.0.0.1',
                    'TEAM_HUB_DIRECT_IP_URL=',
                    'wait_for_exact_release_health() { printf "%s" "${14}"; }',
                    'wait_for_release_health() {' + function,
                    'wait_for_release_health',
                ])
                result = subprocess.run(['/bin/bash', '-c', script], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)


if __name__ == '__main__':
    unittest.main()
