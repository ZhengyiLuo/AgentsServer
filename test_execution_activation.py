"""Outer installer service boundary tests; native commands are explicit test doubles."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import activation_transaction as activation
import execution_activation as bridge
import test_activation_transaction as fixtures


class ExecutionActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.item = fixtures.ActivationLayout(self.base / "fixture")
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.item.service.unlink()
        self.item.service = self.home / ".config/systemd/user/agents-server.service"
        self.item.service.parent.mkdir(parents=True, mode=0o700)
        self.item.write_file(self.item.service, self.item.original_service, 0o644)
        self.tests = fixtures.ActivationTransactionTests()
        original = self.tests.invoke
        self.tests.invoke = lambda *args: original(*args, *(
            ["--execution-runtime-dir", str(self.state / "execution"),
             "--gateway-service", str(self.item.service.with_name("agents-server-gateway.service")),
             "--gateway-state", "absent", "--gateway-enabled", "false", "--execution-api-contract", "28"]
            if args[0] == "begin" else []))
        self.identifier = self.tests.begin(self.item)
        self.tests.activate_to_linked(self.item, self.identifier)
        (self.item.release_dir / ".venv/bin").mkdir(parents=True)
        (self.item.release_dir / ".venv/bin/python").symlink_to(sys.executable)
        (self.item.release_dir / "execution_service.py").write_text("# runtime fixture\n")
        self.args = argparse.Namespace(command="verify", managed_update_id="11111111-1111-4111-8111-111111111111", root=str(self.item.root), config_root=str(self.item.config_root),
            state_root=str(self.state), home=str(self.home), platform="Linux", release_dir=str(self.item.release_dir),
            bind="127.0.0.1", port=7850, health_file="", update_file="", expected_native_pid=8123,
            expected_server_identity="preserved-server-identity")
        self.services = mock.Mock()
        self.services.snapshot.return_value = {"worker": {"state": "running", "pid": 8123},
                                               "gateway": {"state": "running", "pid": 8124}}
        self.control = mock.Mock()

    def value(self):
        return activation.execution_context(self.item.root)

    def json_file(self, path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value)); path.chmod(0o600)
        return str(path)

    def test_publication_uses_outer_snapshots_and_preserves_control_token_on_retry(self):
        bridge.publish(self.args, self.value())
        token = self.state / "execution/control.token"
        before = token.read_bytes(), token.stat().st_ino
        self.assertEqual(len(before[0]), 64)
        bridge.publish(self.args, self.value())
        self.assertEqual((token.read_bytes(), token.stat().st_ino), before)
        manifest = json.loads((self.item.root / "execution-layout.json").read_text())
        self.assertEqual(manifest["layout"]["state_root"], str(self.state))
        self.assertIn(str(self.item.release_dir).encode(), self.item.service.read_bytes())
        self.assertFalse((self.item.root / ".execution-transaction").exists())
        self.tests.rollback(self.item, self.identifier)
        self.assertEqual(self.item.service.read_bytes(), self.item.original_service)
        self.assertFalse((self.item.root / "execution-layout.json").exists())
        self.assertFalse(self.item.service.with_name("agents-server-gateway.service").exists())

    def test_existing_worker_requires_exact_sealed_epoch_before_any_stop(self):
        self.json_file(self.state / "execution/worker.json", {})
        value = self.value(); operation = value["execution"]["operation_id"]
        record = {"pid":8123, "instance_id":"old-epoch", "release_root":str(self.item.old_source)}
        lease = {"operation_id":operation, "lease_id":"lease", "sealed":True}
        self.control.status.return_value = record, {"lease":lease, "idle":True}
        bridge.verify_stop(self.args, value, self.services, self.control)
        for change in ({"operation_id":"foreign"}, {"sealed":False}):
            self.control.status.return_value = record, {"lease":{**lease, **change}, "idle":True}
            with self.assertRaises(RuntimeError):
                bridge.verify_stop(self.args, value, self.services, self.control)
        self.services.stop.assert_not_called()

    def test_legacy_monolith_requires_authenticated_exact_idle_durable_update(self):
        update = "11111111-1111-4111-8111-111111111111"
        self.json_file(self.state / "admin/server-update.json", {"update_id":update, "phase":"installing"})
        health = {"active_count":0, "update_blocking_queued_count":0,
                  "server_identity":self.args.expected_server_identity, "server_update":{"update_id":update},
                  "update_service_cgroup":{"safe":True,"unknown_descendant_count":0}}
        self.args.health_file = self.json_file(self.state / "health.json", health)
        self.args.update_file = self.json_file(self.state / "update-proof.json", {
            "update_id":update,"phase":"installing","server_identity":self.args.expected_server_identity})
        bridge.verify_stop(self.args, self.value(), self.services, self.control)
        for change in ({"active_count":1}, {"update_blocking_queued_count":1},
                       {"server_update":{"update_id":"foreign"}}, {"server_identity":"foreign"}):
            self.json_file(Path(self.args.health_file), {**health, **change})
            with self.assertRaises(RuntimeError):
                bridge.verify_stop(self.args, self.value(), self.services, self.control)
        self.services.stop.assert_not_called()
        # beta9 exposes the native admin status but no health projection.
        legacy = {key:value for key,value in health.items() if key != "server_update"}
        self.json_file(Path(self.args.health_file), legacy)
        bridge.verify_stop(self.args, self.value(), self.services, self.control)
        self.json_file(Path(self.args.update_file), {"update_id":"foreign", "phase":"installing", "server_identity":self.args.expected_server_identity})
        with self.assertRaises(RuntimeError):
            bridge.verify_stop(self.args, self.value(), self.services, self.control)

    def test_missing_legacy_id_requires_exact_live_runner_ancestry(self):
        self.args.managed_update_id = ""
        status = {"update_id":"a"*32, "runner_pid":os.getppid()}
        self.assertEqual(bridge.admitted_update_id(self.args, status), "a"*32)
        with mock.patch.object(bridge.subprocess, "check_output", return_value="1\n"):
            with self.assertRaisesRegex(RuntimeError, "descended"):
                bridge.admitted_update_id(self.args, {**status,"runner_pid":99999999})
        with self.assertRaisesRegex(RuntimeError, "authority"):
            bridge.admitted_update_id(self.args, {"update_id":"a"*32})

    def test_candidate_api_pin_is_read_without_executing_server(self):
        source = self.item.release_dir / "agent_server.py"
        source.write_text('raise RuntimeError("must not run")\nAPI_CONTRACT_VERSION = 28\n')
        self.assertEqual(bridge.candidate_api_contract(self.item.release_dir),28)
        for content in ('API_CONTRACT_VERSION = True\n','API_CONTRACT_VERSION = 0\n',
                        'API_CONTRACT_VERSION = compute()\n','API_CONTRACT_VERSION = 28\nAPI_CONTRACT_VERSION = 29\n'):
            source.write_text(content)
            with self.assertRaises(RuntimeError):
                bridge.candidate_api_contract(self.item.release_dir)

    def test_legacy_recovery_intent_is_bound_to_exact_staged_inode_and_status_cas(self):
        update = "a"*32
        self.args.managed_update_id = update
        self.args.release_version = "2.0.0"
        self.args.api_contract = 28
        self.args.candidate_source = str(self.item.release_dir)
        status_path = self.state / "admin/server-update.json"
        initial = {"update_id":update,"phase":"installing","target_version":"2.0.0","runner_pid":os.getppid()}
        self.json_file(status_path,initial)
        self.args.update_file = self.json_file(self.state / "update-proof.json", {"update_id":update})
        bridge.seed_legacy_recovery(self.args,self.services)
        value=json.loads(status_path.read_text())
        intent=value["_activation_recovery"]
        self.assertEqual(intent["candidate_binding"]["inode"],self.item.release_dir.stat().st_ino)
        self.assertEqual(intent["api_contract"],28)
        bridge.seed_legacy_recovery(self.args,self.services)
        self.assertEqual(json.loads(status_path.read_text())["_activation_recovery"],intent)
        self.json_file(status_path,{**value,"update_id":"b"*32})
        before=status_path.read_bytes()
        with self.assertRaises(RuntimeError):
            bridge.seed_legacy_recovery(self.args,self.services)
        self.assertEqual(status_path.read_bytes(),before)

    def test_candidate_health_requires_both_paired_versions_and_exact_native_pids(self):
        value = self.value()
        record = {"pid":8123,"instance_id":"candidate-epoch","release_root":str(self.item.release_dir)}
        self.control.worker_record.return_value = record
        self.control.status.return_value = record, {"lease":{"sealed":True,"operation_id":value["execution"]["operation_id"]}}
        health = {"gateway":{"version":"2.0.0","protocol":1,"pid":8124},
                  "execution_service":{"version":"2.0.0","protocol":1,"pid":8123,"instance_id":"candidate-epoch"}}
        self.args.health_file = self.json_file(self.state / "health.json", health)
        bridge.verify_components(self.args, value, self.services, self.control)
        for component, changes in (("gateway",{"version":"1.0.0"}), ("execution_service",{"version":"1.0.0"}),
                                   ("gateway",{"pid":99}), ("execution_service",{"instance_id":"foreign"})):
            altered = {**health, component:{**health[component], **changes}}
            self.json_file(Path(self.args.health_file), altered)
            with self.assertRaises(RuntimeError):
                bridge.verify_components(self.args, value, self.services, self.control)

    def test_mutation_requires_exact_installer_lock_ancestor(self):
        lock = self.item.root / ".install-lock"; lock.mkdir(mode=0o700)
        (lock / "pid").write_text(str(os.getppid())+"\n"); (lock / "pid").chmod(0o600)
        self.assertEqual(bridge.transaction(self.args)["transaction_id"], self.identifier)
        (lock / "pid").write_text("99999999\n")
        with mock.patch.object(bridge.subprocess, "check_output", return_value="1\n"):
            with self.assertRaisesRegex(RuntimeError,"owning installer"):
                bridge.transaction(self.args)

    def test_split_service_rendering_always_initializes_final_setup_result_kind(self):
        import shlex, subprocess
        source = (Path(__file__).parent / "install.sh").read_text()
        branch = source[source.index("write_service_files() {"):source.index('  local service_temp=""',source.index("write_service_files() {"))] + "}\n"
        for platform, expected in (("Linux", "systemd-user"), ("Darwin", "launch-agent")):
            script = ('set -eu\nEXECUTION_MODE=split\nCANDIDATE_RUNTIME_ROOT=/candidate\n'
                + 'OS_NAME='+platform+'\nexecution_activation_command() { :; }\n'
                + branch + 'write_service_files\nprintf "%s" "$SERVICE_KIND"')
            result = subprocess.run(['/bin/bash','-c',script],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout,expected)

    @unittest.skipUnless(sys.platform == "darwin", "actual plist parser is macOS-specific")
    def test_legacy_macos_custom_root_mismatch_refuses_before_staging(self):
        import plistlib, shlex, subprocess
        source=(Path(__file__).parent/'install.sh').read_text()
        block=source[source.index('validate_legacy_macos_update_roots() {'):source.index('\nfresh_install_scaffold_is_empty() {')]
        plist=self.base/'legacy.plist'
        plist.write_bytes(plistlib.dumps({'EnvironmentVariables':{'AGENTS_SERVER_INSTALL_DIR':str(self.item.root),'AGENTSDOCK_STATE_DIR':str(self.state)}}));plist.chmod(0o600)
        prefix='set -eu\nOS_NAME=Darwin\nEXPECTED_SERVER_IDENTITY=owned-server\nLABEL=com.agentsdock.server\nPLIST='+shlex.quote(str(plist))+'\nlaunchctl() { :; }\nSTATE_ROOT='+shlex.quote(str(self.state))+'\n'
        for root,success in ((self.item.root,True),(self.base/'wrong-root',False)):
            result=subprocess.run(['/bin/bash','-c',prefix+'INSTALL_ROOT='+shlex.quote(str(root))+'\n'+block],capture_output=True,text=True)
            self.assertEqual(result.returncode==0,success,result.stderr)
            if not success:self.assertIn('original configuration path cannot be inferred',result.stderr)
        self.assertFalse((self.base/'wrong-root').exists())

    def test_finalization_receipt_follows_lease_release_and_journal_retirement(self):
        import shlex, subprocess
        source=(Path(__file__).parent/'install.sh').read_text()
        block=source[source.index('finish_activation_transaction() {'):source.index('\nassert_env_backup_team_hub_config() {')]
        log=self.base/'finalization-order'
        prefix='set -eu\nEXECUTION_MODE=split\nCANDIDATE_RUNTIME_ROOT=/candidate\nINSTALL_ROOT=/install\nCURRENT_LINK=/install/current\nPREVIOUS_LINK=/install/previous\nENV_FILE=/config/env\nRELEASE_DIR=/install/releases/2.0.0\nRELEASE_VERSION=2.0.0\nACTIVATION_TRANSACTION_ID=activation-0123456789abcdef01234567\n'
        prefix+='LOG='+shlex.quote(str(log))+'\n'
        prefix+='execution_activation_command() { printf "lease:%s\\n" "$1" >> "$LOG"; }\n'
        prefix+='execution_recovery_command() { printf "owner:%s\\n" "$1" >> "$LOG"; }\n'
        prefix+='activation_service_config_path() { printf /service; }\n'
        prefix+='activation_transaction_command() { printf "journal:%s\\n" "$2" >> "$LOG"; }\n'
        result=subprocess.run(['/bin/bash','-c',prefix+block+'\nfinish_activation_transaction /candidate\ntest -z "$ACTIVATION_TRANSACTION_ID"\n'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(log.read_text().splitlines(),['lease:release','owner:complete','journal:finish','owner:finalized'])

    def test_legacy_admission_fetches_native_admin_header_before_stop(self):
        import shlex, subprocess
        source = (Path(__file__).parent / "install.sh").read_text()
        branch = source[source.index("execution_stop_services() {"):source.index("backup_runtime_configuration() {")]
        (self.state/"admin").mkdir(exist_ok=True)
        log = self.base/"proof-requests"
        script = ('set -eu\nSTATE_ROOT='+shlex.quote(str(self.state))+'\n'
            + 'CANDIDATE_RUNTIME_ROOT=/candidate\nRELEASE_VERSION=2.0.0\nSERVICE_NAME=agents-server\nLEGACY_SERVICE_NAME=zenithbot-agent\n'
            + 'OS_NAME=Linux\nPORT=7850\nBIND_ADDRESS=127.0.0.1\nMANAGED_UPDATE_ID=owned-update\nEXECUTION_HANDOFF_FILE=\n'
            + 'service_manager_main_pid() { [[ "$1" == agents-server ]] && printf "123\\n"; }\n'
            + 'fetch_managed_json() { printf "%s:%s\\n" "$2" "$3" >> '+shlex.quote(str(log))+'; printf "{}" > "$4"; }\n'
            + 'execution_activation_command() { [[ "$1" == preflight ]]; }\n'
            + branch + 'execution_stop_services /candidate preflight\n')
        result = subprocess.run(['/bin/bash','-c',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(log.read_text().splitlines(),['/api/health:core','/api/admin/update:native'])
        self.assertEqual(list((self.state/'admin').iterdir()),[])
