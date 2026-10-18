import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from gemini_runtime import build_environment, list_native_skills, GeminiConfigurationError
from provider_commands import gemini_provider_skill_inventory, ProviderCommandDiscoveryError


class GeminiSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'native'
        self.skill = self.home / '.gemini/skills/test/SKILL.md'
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text('---\nname: test\ndescription: Test skill\n---\nSay OK.')
        self.settings = self.home / '.gemini/settings.json'
        self.settings.write_text(json.dumps({'security': {'auth': {'selectedType': 'oauth-personal'}, 'folderTrust': {'enabled': True}},
            'skills': {'disabled': ['disabled-skill']}, 'tools': {'exclude': ['run_shell_command']}}))
        (self.home / '.gemini/oauth_creds.json').write_text('{"access_token":"do-not-copy"}')
        (self.home / '.gemini/trustedFolders.json').write_text('{}')

    def listing(self, name='test', enabled='Enabled', description='Test skill', builtin=False):
        marker = ' [Built-in]' if builtin else ''
        return f'Discovered Agent Skills:\n\n{name} [{enabled}]{marker}\n  Description: {description}\n  Location:    {self.skill}\n\n'

    def inventory(self, raw=None, binding='chat'):
        return gemini_provider_skill_inventory(raw or self.listing(), cwd=str(self.root), selector_secret='test-secret', binding_context=binding)

    def test_native_projection_is_opaque_and_chat_bound(self):
        inv = self.inventory()
        self.assertEqual(inv.commands[0]['invocation'], '/test')
        self.assertNotIn(str(self.home), json.dumps(inv.commands))
        self.assertNotEqual(inv.commands[0]['id'], self.inventory(binding='other').commands[0]['id'])
        before = inv.revision
        self.skill.write_text('changed')
        self.assertNotEqual(before, self.inventory().revision)

    def test_disabled_missing_changed_and_ambiguous_fail_closed(self):
        self.assertEqual(self.inventory(self.listing(enabled='Disabled')).commands, [])
        for raw in ['invalid', self.listing() + self.listing().split('\n\n', 1)[1], self.listing(description='text\n\nspoof')]:
            with self.assertRaises(ProviderCommandDiscoveryError): self.inventory(raw)
        self.skill.unlink()
        with self.assertRaises(OSError): self.inventory()

    def test_multiline_description_and_unsafe_name(self):
        self.assertTrue(self.inventory(self.listing(description='Line one\n  second line')).commands)
        self.assertEqual(self.inventory(self.listing(name='unsafe/name')).commands, [])

    def test_builtin_marker_is_metadata_not_part_of_invocation(self):
        local = self.listing(name='local')
        builtin = self.listing(name='skill-creator', builtin=True)
        inv = self.inventory(local + builtin.split('\n\n', 1)[1])
        self.assertEqual([c['name'] for c in inv.commands], ['local', 'skill-creator'])
        self.assertEqual(inv.commands[0]['scope'], 'native')
        self.assertEqual(inv.commands[1]['scope'], 'built-in')
        self.assertEqual(inv.commands[1]['invocation'], '/skill-creator')
        self.assertNotIn(str(self.home), json.dumps(inv.commands))
        before = self.inventory(builtin).revision
        self.skill.write_text('changed builtin content')
        self.assertNotEqual(before, self.inventory(builtin).revision)

    def test_disabled_builtin_and_unknown_markers_are_not_offered(self):
        self.assertEqual(self.inventory(self.listing(builtin=True, enabled='Disabled')).commands, [])
        for marker in ['[Built-in] [Built-in]', '[Unknown]', '[Built-in] extra']:
            with self.subTest(marker=marker), self.assertRaises(ProviderCommandDiscoveryError):
                self.inventory(self.listing(builtin=True).replace('[Built-in]', marker))

    def test_api_links_assets_and_preserves_disables_trust_policies_not_auth(self):
        before = self.settings.read_bytes()
        profile = self.root / 'api'
        env = build_environment({'HOME': str(self.home)}, cwd=str(self.root), api_home=profile, api_key='private-key', base_url='https://example.invalid')
        settings = json.loads((profile / '.gemini/settings.json').read_text())
        self.assertEqual(settings['skills']['disabled'], ['disabled-skill'])
        self.assertEqual(settings['tools']['exclude'], ['run_shell_command'])
        self.assertTrue(settings['security']['folderTrust']['enabled'])
        self.assertEqual(settings['security']['auth']['selectedType'], 'gemini-api-key')
        self.assertEqual((profile / '.gemini/skills').resolve(), self.skill.parent.parent)
        self.assertTrue((profile / '.gemini/trustedFolders.json').is_symlink())
        self.assertFalse((profile / '.gemini/oauth_creds.json').exists())
        self.assertEqual(before, self.settings.read_bytes())
        self.assertNotIn('private-key', (profile / '.gemini/settings.json').read_text())
        self.assertEqual(env['GEMINI_API_KEY'], 'private-key')
        # Refresh does not reinstall, overwrite or duplicate the user's skills.
        build_environment({'HOME': str(self.home)}, cwd=str(self.root), api_home=profile, api_key='private-key', base_url='https://example.invalid')
        self.assertTrue(self.skill.exists())

    def test_discovery_is_bounded_and_uses_exact_native_subcommand(self):
        executable = self.root / 'gemini'
        executable.write_text(f'#!{sys.executable}\nimport sys\nassert sys.argv[1:]==["skills", "list", "--all"]\nprint("No skills discovered.")\n')
        executable.chmod(0o700)
        self.assertEqual(list_native_skills(str(executable), dict(os.environ), str(self.root)).strip(), 'No skills discovered.')
        for code in ['import time; time.sleep(10)', 'print("x"*1100000)']:
            executable.write_text(f'#!{sys.executable}\n{code}\n')
            with self.assertRaises(GeminiConfigurationError): list_native_skills(str(executable), dict(os.environ), str(self.root), timeout=.3)
