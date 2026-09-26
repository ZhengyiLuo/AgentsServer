"""Native model metadata only: no server import, account, or model calls."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import claude_model_catalog as catalog


EXPANSION_CLI = '''
import json, os, pathlib, sys, time
args = sys.argv[1:]
settings = json.loads(args[args.index('--settings')+1])
assert settings['disableAllHooks'] is True
assert 'availableModels' not in settings and 'env' not in settings
assert '--no-session-persistence' in args
assert args[args.index('--tools')+1] == ''
assert args[args.index('--mcp-config')+1] == '{"mcpServers":{}}'
requests = [json.loads(line) for line in sys.stdin]
assert all(r['type'] == 'control_request' for r in requests)
count = pathlib.Path(os.environ['TEST_ROOT'], 'runs')
count.write_text(str(int(count.read_text())+1 if count.exists() else 1))
expanded = 'modelPicker' in settings
if expanded:
    assert len(requests) == 1
    candidates = [r['model'] for r in settings['modelPicker']['options']]
    if os.environ.get('TEST_TIMEOUT'): time.sleep(30)
    if os.environ.get('TEST_FAILURE'): sys.exit(1)
    models = json.loads(os.environ.get('TEST_EXPANDED', '[]'))
    for model in models:
        if model.get('value') not in ('opus', 'custom', 'unrequested'):
            assert model['value'] in candidates
    effective = json.loads(os.environ.get('TEST_EFFECTIVE', '{}'))
    for row in effective.get('modelPicker', {}).get('options', []):
        assert row in settings['modelPicker']['options']
else:
    models = [{'value':'opus', 'displayName':'Opus', 'resolvedModel':'claude-opus-5-5'},
              {'value':'custom','displayName':'My custom model'}]
for req in reversed(requests):
    if req['request']['subtype'] == 'initialize':
        info = {'models':models, 'account':{'apiProvider':os.environ.get('TEST_PROVIDER', 'firstParty'),
                                          'email':'private@example.com'}}
    else:
        assert req['request']['subtype'] == 'get_settings'
        if os.environ.get('TEST_NO_SETTINGS'): continue
        info = {'effective':json.loads(os.environ.get('TEST_EFFECTIVE', '{}')),
                'private':'never forward raw settings'}
    print(json.dumps({'type':'control_response','response':{'subtype':'success',
                     'request_id':req['request_id'],'response':info}}), flush=True)
'''


class NativeExpansionSettingsTests(unittest.TestCase):
    def test_only_picker_and_hook_isolation_are_overridden(self):
        existing = {"model": "company/model", "label": "Company", "behavesAs": "claude-opus-5"}
        effective = {"availableModels": ["sonnet"], "permissions": {"deny": ["Bash"]},
                     "env": {"PRIVATE": "secret"}, "modelPicker": {"options": [existing]}}
        result = catalog._expansion_settings(effective, {}, ("claude-opus-5",))
        self.assertEqual(result, {"disableAllHooks": True, "modelPicker": {
            "options": [existing, {"model": "claude-opus-5"}]}})
        self.assertEqual(len(effective["modelPicker"]["options"]), 1)

    def test_unknown_or_custom_configuration_does_not_get_anthropic_seed(self):
        for effective in (None, [], {"env": None}, {"env": {"ANTHROPIC_BASE_URL": "https://gateway.test"}},
                          {"modelPicker": {"options": [], "replaceBuiltInOptions": True}},
                          {"modelPicker": {"options": [None]}},
                          {"modelPicker": {"options": [{"model": "x", "label": "X" * 65536}]}}):
            with self.subTest(effective=str(effective)[:80]):
                self.assertIsNone(catalog._expansion_settings(effective, {}, ("claude-opus-5",)))
        for env in ({"ANTHROPIC_BASE_URL": "https://gateway.test"}, {"CLAUDE_CODE_USE_BEDROCK": "1"}):
            self.assertIsNone(catalog._expansion_settings({}, env, ("claude-opus-5",)))
        self.assertIsNone(catalog._expansion_settings({}, {}, ()))


class NativeModelLabelTests(unittest.TestCase):
    def parse(self, *models):
        return catalog.parse_native_models({"models": list(models)})

    def test_versioned_alias_keeps_value_and_follows_future_versions(self):
        for version in ("5-5", "6", "6-1", "10-12"):
            with self.subTest(version=version):
                self.assertEqual(self.parse({
                    "value": "opus", "displayName": "Opus",
                    "resolvedModel": "claude-opus-" + version,
                }), [{"value": "opus", "label": "Opus " + version.replace("-", ".")}])

    def test_current_native_picker_default_context_and_dated_model(self):
        rows = self.parse(
            {"value": "default", "displayName": "Default (recommended)", "resolvedModel": "claude-opus-5-5[1m]"},
            {"value": "opus[1m]", "displayName": "Opus (1M context)", "resolvedModel": "claude-opus-5-5[1m]"},
            {"value": "claude-fable-5-1[1m]", "displayName": "Fable", "resolvedModel": "claude-fable-5-1"},
            {"value": "sonnet", "displayName": "Sonnet", "resolvedModel": "claude-sonnet-5"},
            {"value": "haiku", "displayName": "Haiku", "resolvedModel": "claude-haiku-4-5-20251001"},
        )
        self.assertEqual([r["label"] for r in rows], [
            "Default — Opus 5.5 (1M context)", "Opus 5.5 (1M context)",
            "Fable 5.1 (1M context)", "Sonnet 5", "Haiku 4.5",
        ])

    def test_old_sdk_uses_native_description_without_guessing(self):
        self.assertEqual(self.parse(
            {"value": "opus", "displayName": "Opus", "description": "Opus 4.8 · Most capable"},
            {"value": "sonnet", "displayName": "Sonnet", "description": "Sonnet 5 with 1M context · Fast"},
            {"value": "default", "displayName": "Default", "description": "Opus 5.5 · Recommended"},
        ), [
            {"value": "opus", "label": "Opus 4.8"},
            {"value": "sonnet", "label": "Sonnet 5 (1M context)"},
            {"value": "default", "label": "Default — Opus 5.5"},
        ])

    def test_unknown_gateway_ids_keep_native_name(self):
        self.assertEqual(self.parse({
            "value": "opus", "resolvedModel": "company/prod-v2", "displayName": "Company deployment",
            "description": "Opus 5.5 · Not an authoritative mapping for this gateway",
        }), [{"value": "opus", "label": "Company deployment"}])

    def test_missing_resolution_and_description_does_not_invent_version(self):
        self.assertEqual(self.parse({"value": "opus", "displayName": "Opus"}),
                         [{"value": "opus", "label": "Opus"}])
        self.assertEqual(self.parse({"value": "custom", "displayName": "Custom",
                                    "description": "Faster than Opus 5.5"}),
                         [{"value": "custom", "label": "Custom"}])

    def test_empty_list_is_authoritative_but_missing_schema_is_not(self):
        self.assertEqual(self.parse(), [])
        for invalid in (None, {}, {"models": None}, {"models": {}}, {"models": [None]}):
            with self.subTest(invalid=invalid), self.assertRaises(catalog.ClaudeModelCatalogUnavailable):
                catalog.parse_native_models(invalid)

    def test_bad_rows_duplicates_and_private_metadata_are_not_forwarded(self):
        rows = catalog.parse_native_models({
            "account": {"email": "private@example.com"}, "commands": ["private-command"],
            "models": [None, {"value": "bad id"}, {"value": "bad\n"}, {"value": 42},
                       {"value": "sonnet", "displayName": "Sonnet", "resolvedModel": "claude-sonnet-5",
                        "private": "secret"}, {"value": "sonnet", "displayName": "Duplicate"}],
        })
        self.assertEqual(rows, [{"value": "sonnet", "label": "Sonnet 5"}])

    def test_disabled_rows_are_not_selectable_including_an_all_disabled_list(self):
        self.assertEqual(self.parse({"value": "opus", "disabled": True}), [])
        self.assertEqual(self.parse({"value": "opus", "disabled": True},
                                    {"value": "sonnet", "displayName": "Sonnet"}),
                         [{"value": "sonnet", "label": "Sonnet"}])

    def test_unsafe_or_unbounded_display_fields_fall_back_to_id(self):
        for name in ("Bad\nName", "Bad\u202eName", "x" * 161, 123):
            with self.subTest(name=name):
                self.assertEqual(self.parse({"value": "custom", "displayName": name}),
                                 [{"value": "custom", "label": "custom"}])
        with self.assertRaises(catalog.ClaudeModelCatalogUnavailable):
            catalog.parse_native_models({"models": [{}] * (catalog.MAX_MODELS + 1)})
        self.assertEqual(len(self.parse({"value": "x" * 256})[0]["label"]), 160)
        self.assertEqual(self.parse({"value": "opus", "displayName": "Opus",
                                    "description": "Opus 5 with " + "X" * 200 + " context · Example"}),
                         [{"value": "opus", "label": "Opus"}])


@unittest.skipUnless(os.name == "posix", "POSIX server runtime process groups")
class NativeModelProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="native-model-test-"))
        self.root = Path(self.tmp)
        self.executable = self.root / "fake-claude"
        self.env = {"PATH": os.environ.get("PATH", ""), "TEST_ROOT": str(self.root)}

    def script(self, body):
        self.executable.write_text("#!" + sys.executable + "\n" + body)
        self.executable.chmod(0o700)
        return str(self.executable)

    def probe(self, body, timeout=2):
        return catalog.probe_native_models(self.script(body), env=self.env, timeout=timeout)

    def test_initialize_only_no_tools_hooks_mcp_history_or_account_output(self):
        rows = self.probe('''
import json, os, pathlib, sys
args = sys.argv[1:]
assert '--no-session-persistence' in args and '--strict-mcp-config' in args
assert args[args.index('--mcp-config')+1] == '{"mcpServers":{}}'
assert args[args.index('--tools')+1] == ''
assert args[args.index('--setting-sources')+1] == 'user'
assert json.loads(args[args.index('--settings')+1])['disableAllHooks'] is True
assert os.environ['DISABLE_AUTOUPDATER'] == '1'
assert str(pathlib.Path.cwd()) != os.environ['TEST_ROOT']
messages = [json.loads(line) for line in sys.stdin]
assert len(messages) == 2
assert messages[1]['request']['subtype'] == 'get_settings'
req = messages[0]
assert req['type'] == 'control_request' and req['request']['subtype'] == 'initialize'
assert req['request']['skills'] == []
print('private stderr', file=sys.stderr)
print(json.dumps({'type':'control_response', 'response': {
    'subtype':'success', 'request_id': req['request_id'], 'response': {
        'account': {'email': 'private@example.com'}, 'models': [
            {'value':'opus', 'resolvedModel':'claude-opus-5-5', 'displayName':'Opus'}
        ]}}}))
''')
        self.assertEqual(rows, [{"value": "opus", "label": "Opus 5.5"}])

    def test_timeout_kills_and_reaps_probe(self):
        created = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            created.append(process)
            return process
        with patch.object(catalog.subprocess, "Popen", side_effect=spawn):
            started = time.monotonic()
            with self.assertRaisesRegex(catalog.ClaudeModelCatalogUnavailable, "timed out"):
                self.probe("import time\ntime.sleep(30)\n", timeout=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(created[0].poll())

    def test_output_limit_and_non_json_trickle_are_bounded(self):
        with patch.object(catalog, "MAX_OUTPUT_BYTES", 512):
            with self.assertRaisesRegex(catalog.ClaudeModelCatalogUnavailable, "limit"):
                self.probe("import sys,time\nsys.stdout.write('x'*1024);sys.stdout.flush();time.sleep(30)\n")
        with self.assertRaisesRegex(catalog.ClaudeModelCatalogUnavailable, "timed out"):
            self.probe("import sys,time\nwhile True:\n print('{}', flush=True);time.sleep(0.01)\n", timeout=0.2)

    def test_success_also_stops_owned_descendants_and_removes_temporary_cwd(self):
        self.probe('''
import json, os, pathlib, subprocess, sys, time
req=json.loads(sys.stdin.readline())
child=subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
pathlib.Path(os.environ['TEST_ROOT'], 'child.json').write_text(json.dumps({
 'pid':child.pid, 'cwd':str(pathlib.Path.cwd())}))
print(json.dumps({'type':'control_response','response':{'subtype':'success',
 'request_id':req['request_id'],'response':{'models':[]}}}), flush=True)
time.sleep(30)
''')
        child = json.loads((self.root / "child.json").read_text())
        self.assertFalse(Path(child["cwd"]).exists())
        # Reparented children may briefly be zombies until init reaps them;
        # neither a zombie nor an absent PID is a surviving runtime process.
        for _ in range(50):
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(child["pid"])],
                                   text=True, capture_output=True, timeout=1).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.02)
        self.assertTrue(not state or state.startswith("Z"), state)

    def test_cli_upgrade_is_observed_on_next_probe_without_server_restart(self):
        body = '''
import json,sys
req=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{'subtype':'success',
 'request_id':req['request_id'],'response':{'models':[{'value':'opus',
 'displayName':'Opus','resolvedModel':'claude-opus-VERSION'}]}}}))
'''
        self.assertEqual(self.probe(body.replace("VERSION", "5-5"))[0]["label"], "Opus 5.5")
        self.assertEqual(self.probe(body.replace("VERSION", "6-1"))[0]["label"], "Opus 6.1")

    def test_protocol_rejection_never_exposes_private_error(self):
        with self.assertRaises(catalog.ClaudeModelCatalogUnavailable) as error:
            self.probe('''
import json, sys
req=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response': {'subtype':'error',
 'request_id':req['request_id'],'error':'private@example.com token=secret'}}))
''')
        self.assertNotIn("private", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    def test_unrelated_response_does_not_supply_models(self):
        with self.assertRaises(catalog.ClaudeModelCatalogUnavailable):
            self.probe('''
import json
print(json.dumps({'type':'control_response','response': {'subtype':'success',
 'request_id':'other','response':{'models':[]}}}))
''')

    def test_busy_and_expired_probe_never_spawns(self):
        with catalog._PROBE_LOCK, patch.object(catalog.subprocess, "Popen") as popen:
            with self.assertRaises(catalog.ClaudeModelCatalogUnavailable):
                catalog.probe_native_models("unused", env={}, timeout=0.01)
        popen.assert_not_called()
        with patch.object(catalog.subprocess, "Popen") as popen:
            with self.assertRaises(catalog.ClaudeModelCatalogUnavailable):
                catalog.probe_native_models("unused", env={}, timeout=0)
        popen.assert_not_called()

    def test_expansion_keeps_new_old_custom_and_filters_disabled_or_absent(self):
        self.env['TEST_EFFECTIVE'] = json.dumps({"availableModels": ["opus", "custom"],
            "modelPicker": {"options": [{"model": "custom", "label": "My custom model"}]}})
        self.env['TEST_EXPANDED'] = json.dumps([
            {"value":"opus", "resolvedModel":"claude-opus-5-5", "displayName":"Opus"},
            {"value":"custom", "displayName":"My custom model"},
            {"value":"claude-opus-5", "resolvedModel":"claude-opus-5", "displayName":"Opus 5"},
            {"value":"claude-opus-4-8", "disabled":True},
            {"value":"unrequested", "displayName":"Do not add"},
        ])
        rows = self.probe(EXPANSION_CLI)
        self.assertEqual(rows, [
            {"value":"opus", "label":"Opus 5.5"},
            {"value":"custom", "label":"My custom model"},
            {"value":"claude-opus-5", "label":"Opus 5"},
        ])
        self.assertEqual((self.root/'runs').read_text(), '2')
        self.assertNotIn('private', json.dumps(rows))

    def test_expanded_empty_list_does_not_resurrect_baseline_or_seed(self):
        self.assertEqual(self.probe(EXPANSION_CLI), [])

    def test_api_candidates_replace_seed_and_are_not_added_when_native_omits_them(self):
        self.env['TEST_EXPANDED'] = json.dumps([{"value":"claude-api-custom", "displayName":"API custom"}])
        rows = catalog.probe_native_models(self.script(EXPANSION_CLI), env=self.env,
                                            timeout=2, candidates=("claude-api-custom", "claude-api-denied"))
        self.assertEqual(rows, [{"value":"claude-api-custom", "label":"API custom"}])

    def test_empty_api_candidates_do_not_fall_back_to_seed(self):
        rows = catalog.probe_native_models(self.script(EXPANSION_CLI), env=self.env, timeout=2, candidates=())
        self.assertEqual([r['value'] for r in rows], ['opus', 'custom'])
        self.assertEqual((self.root/'runs').read_text(), '1')

    def test_expansion_failure_preserves_working_native_list(self):
        self.env['TEST_FAILURE'] = '1'
        self.assertEqual([r['value'] for r in self.probe(EXPANSION_CLI)], ['opus', 'custom'])

    def test_expansion_and_baseline_share_one_deadline(self):
        self.env['TEST_TIMEOUT'] = '1'
        start = time.monotonic()
        self.assertEqual([r['value'] for r in self.probe(EXPANSION_CLI, timeout=0.3)], ['opus', 'custom'])
        self.assertLess(time.monotonic() - start, 1.5)

    def test_no_expansion_for_third_party_curated_gateway_or_unknown_settings(self):
        for overrides in ({'TEST_PROVIDER':'bedrock'}, {'TEST_NO_SETTINGS':'1'},
                          {'TEST_EFFECTIVE':json.dumps({'env':{'ANTHROPIC_BASE_URL':'https://gateway.test'}})},
                          {'TEST_EFFECTIVE':json.dumps({'modelPicker':{'options':[], 'replaceBuiltInOptions':True}})}):
            with self.subTest(overrides=overrides), patch.dict(self.env, overrides):
                self.assertEqual([r['value'] for r in self.probe(EXPANSION_CLI)], ['opus', 'custom'])
        self.assertEqual((self.root/'runs').read_text(), '4')


if __name__ == "__main__":
    unittest.main()
