"""Run the installer's actual staging branch with inert dependency resolution."""
from pathlib import Path
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import execution_preparation

ROOT = Path(__file__).resolve().parents[1]


class InstallerPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'install'; self.root.mkdir(mode=0o700)
        self.source = self.base / 'source'; self.source.mkdir(mode=0o700)
        self.names = ['VERSION','agent_server.py','execution_service.py','pyproject.toml','uv.lock',
                      'activation_transaction.py','execution_preparation.py']
        for name in self.names:
            data = (ROOT/name).read_bytes() if name.endswith('_transaction.py') or name == 'execution_preparation.py' else b'# source fixture\n'
            (self.source/name).write_bytes(data)
        (self.source/'VERSION').write_text('2.0.0\n')
        (self.source/'agent_server.py').write_text('API_CONTRACT_VERSION = 28\n')
        # The actual branch copies these mandatory executable helpers.
        for name in ('agentsdock_jobs.py','agentsdock_chats.py','agentsdock_emergency.py','agentsdock_publish.py',
                     'agentsdock_mail.py','agentsdock_team.py','install.sh','uninstall.sh','update_runner.py'):
            self.names.append(name); (self.source/name).write_text('# fixture\n')
        (self.source/"agentsdock_team_hub").mkdir()
        (self.source/"agentsdock_team_hub/__init__.py").write_text("# package fixture\n")
        (self.source/"agentsdock_team_hub/migrations").mkdir()
        (self.source/"agentsdock_team_hub/migrations/001.sql").write_text("-- fixture\n")
        self.receipt = self.root/'.prepared-receipts/test.json'
        self.state = self.base/'state'; self.config = self.base/'config'
        code = (ROOT/'install.sh').read_text()
        self.block = code[code.index('# Recovery never restages'):code.index('\nPRESERVE_SOURCE=""',code.index('# Recovery never restages'))]

    def script(self, *, activate=False, recover=False):
        variables = dict(INSTALL_ROOT=str(self.root), RELEASES_ROOT=str(self.root/'releases'),
            RELEASE_VERSION='2.0.0', REQUESTED_RELEASE_VERSION='2.0.0', EXPECTED_API_CONTRACT='28',
            STAGE_DIR=str(self.root/'releases/.staging-2.0.0-test'), STAGE_DIR_DEVICE='', STAGE_DIR_INODE='',
            CANDIDATE_RUNTIME_ROOT='', SOURCE_DIR=str(self.source), CONFIG_ROOT=str(self.config),
            STATE_ROOT=str(self.state), BIND_ADDRESS='127.0.0.1', INSTALL_LOCK_HELD='true',
            ACTIVATION_TRANSACTION_RESUMED='false', PREPARE_ONLY='false' if activate or recover else 'true',
            RECOVER_ONLY='true' if recover else 'false', EXPECTED_ACTIVATION_ID='', ACTIVATE_PREPARED=str(self.receipt) if activate else '',
            PREPARED_RECEIPT=str(self.receipt), PREPARED_ARCHIVE_SHA256='a'*64,
            UV_BIN='/usr/bin/true', DEPENDENCY_SYNC_TIMEOUT_SECONDS='5')
        setup='set -euo pipefail\n'+''.join(f'{k}={shlex.quote(v)}\n' for k,v in variables.items())
        setup+='RELEASE_FILES=('+ ' '.join(map(shlex.quote,self.names))+')\nTEAM_HUB_RELEASE_FILES=(__init__.py migrations/001.sql)\n'
        setup+='''validate_install_layout_paths() { :; }
run_without_server_secrets() { "$@"; }
validate_bind_address() { :; }
validate_staged_release_runtime() { :; }
run_timed_stage() { shift 3; "$@"; }
'''
        setup+=f'''install_lock_python() {{ printf '%s\\n' {shlex.quote(sys.executable)}; }}
sync_release_dependencies() {{
  mkdir -p "$STAGE_DIR/.venv/bin"
  ln -s {shlex.quote(sys.executable)} "$STAGE_DIR/.venv/bin/python"
}}
migrate_legacy_state() {{ printf '%s\\n' "$STAGE_DIR" > {shlex.quote(str(self.base/'activation-reached'))}; exit 0; }}
'''
        return setup+self.block

    def run_script(self, **kwargs):
        return subprocess.run(['/bin/bash','-c',self.script(**kwargs)], capture_output=True,text=True,
                              env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}, timeout=30)

    def test_prepared_runtime_generated_cache_requires_receipt_path_and_keeps_symlinks_rejected(self):
        code=(ROOT/'install.sh').read_text()
        block=code[code.index('for name in "${RELEASE_FILES[@]}"; do'):code.index('\ncurrent_release_binding()')]
        cache=self.source/'agentsdock_team_hub/__pycache__';cache.mkdir();(cache/'cli.cpython.pyc').write_bytes(b'generated cache')
        prefix='set -eu\nSOURCE_DIR='+shlex.quote(str(self.source))+'\nRELEASE_FILES=(VERSION)\nRELEASE_DIRECTORIES=(agentsdock_team_hub)\nTEAM_HUB_RELEASE_FILES=(__init__.py migrations/001.sql)\n'
        def check(prepared,recover=False):
            return subprocess.run(['/bin/bash','-c',prefix+'ACTIVATE_PREPARED='+shlex.quote(str(self.receipt) if prepared else '')+'\nRECOVER_ONLY='+('true' if recover else 'false')+'\n'+block],capture_output=True,text=True)
        self.assertNotEqual(check(False).returncode,0)
        self.assertEqual(check(True).returncode,0)
        self.assertEqual(check(False,True).returncode,0)
        (cache/'link').symlink_to(self.source/'VERSION')
        self.assertNotEqual(check(True).returncode,0)
        self.assertNotEqual(check(False,True).returncode,0)

    def test_prepare_creates_receipt_without_state_config_service_or_current_changes(self):
        result=self.run_script()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        value=execution_preparation.validate_prepared(root=self.root,version='2.0.0',api_contract=28,
            archive_sha256='a'*64,receipt=self.receipt)
        self.assertFalse(self.state.exists()); self.assertFalse(self.config.exists())
        self.assertFalse((self.root/'current').exists()); self.assertFalse((self.root/'.activation-transaction').exists())
        self.assertTrue(Path(value['candidate']).is_dir())
        self.assertEqual(list(self.root.glob('.prepared-inventory.*')),[])

    def test_activation_reuses_exact_prepared_candidate_and_rejects_changed_dependency(self):
        self.assertEqual(self.run_script().returncode,0)
        candidate=Path(json.loads(self.receipt.read_text())['candidate'])
        result=self.run_script(activate=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.base/'activation-reached').read_text().strip(),str(candidate))
        (self.base/'activation-reached').unlink()
        (candidate/'agent_server.py').write_text('API_CONTRACT_VERSION = 999\n')
        result=self.run_script(activate=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.base/'activation-reached').exists())
        self.assertTrue(candidate.exists())

    def test_recovery_without_journal_refuses_before_staging(self):
        result=self.run_script(recover=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'releases').exists())
        self.assertFalse(self.state.exists()); self.assertFalse(self.config.exists())

    def test_fresh_root_parents_are_private_under_group_writable_user_umask(self):
        code = (ROOT/'install.sh').read_text()
        start = code.index('(umask 077; mkdir -p "$CONFIG_ROOT" "$STATE_ROOT" "$STATE_ROOT/admin")')
        finish = code.index('\nTOKEN=', start)
        config = self.base/'new-home/.config/agents-server'
        state = self.base/'new-home/state'
        result = subprocess.run(['/bin/bash','-c', 'umask 0002\n'
            + 'CONFIG_ROOT='+shlex.quote(str(config))+'\nSTATE_ROOT='+shlex.quote(str(state))+'\n'
            + code[start:finish]], capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        for path in (config, config.parent, config.parent.parent, state, state/'admin'):
            self.assertEqual(path.stat().st_mode & 0o777,0o700)
