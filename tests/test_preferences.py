import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import titles


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        patch = mock.patch.object(titles, 'APP_DIR', self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def test_new_install_defaults_to_luna_extra_high(self):
        with mock.patch.object(titles, 'MODEL', 'gpt-6-luna'), \
             mock.patch.object(titles, 'REASONING', 'xhigh'):
            self.assertEqual(titles.preferences(), dict(model='gpt-6-luna', reasoning='xhigh'))

    def test_saved_choice_overrides_legacy_launch_environment(self):
        with mock.patch.object(titles, 'REASONING', 'max'):
            titles.configure('gpt-6-luna', 'xhigh')
            self.assertEqual(titles.preferences()['reasoning'], 'xhigh')
        self.assertEqual((self.home / 'preferences.json').stat().st_mode & 0o777, 0o600)

    def test_invalid_choices_do_not_replace_settings(self):
        titles.configure('gpt-6-luna', 'xhigh')
        before = (self.home / 'preferences.json').read_bytes()
        for model, effort in [('shell-command', 'high'), ('gpt-6-luna', 'ultra')]:
            with self.assertRaises(ValueError):
                titles.configure(model, effort)
        self.assertEqual((self.home / 'preferences.json').read_bytes(), before)

    def test_corrupt_or_unrecognized_settings_fail_to_known_defaults(self):
        (self.home / 'preferences.json').write_text('not json')
        self.assertIn(titles.preferences()['model'], titles.MODELS)
        titles.atomic_json(self.home / 'preferences.json', dict(model='unknown', reasoning='ultra'))
        self.assertEqual(titles.preferences(), dict(model='gpt-6-luna', reasoning='xhigh'))
        titles.atomic_json(self.home / 'preferences.json', dict(model=[], reasoning={}))
        self.assertEqual(titles.preferences(), dict(model='gpt-6-luna', reasoning='xhigh'))

    def test_settings_do_not_reset_titles_or_review_counters(self):
        cache = self.home / 'titles-v2' / 'existing.json'
        cadence = self.home / 'conversation-reviews-v1' / 'existing.json'
        titles.atomic_json(cache, {'title': 'Existing Task'})
        titles.atomic_json(cadence, {'reviewedTotal': 10})
        titles.configure('gpt-6.1-sol', 'high')
        self.assertEqual(titles.read_json(cache), {'title': 'Existing Task'})
        self.assertEqual(titles.read_json(cadence), {'reviewedTotal': 10})


class GeneratorPreferencesTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_request_uses_saved_selection_without_restart(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(titles, 'APP_DIR', Path(directory)):
            requests = []
            async def create(*args, **kwargs):
                requests.append(args)
                proc = SimpleNamespace(pid=987650, returncode=None)
                async def communicate(_):
                    output = Path(args[args.index('--output-last-message') + 1])
                    output.write_text(json.dumps(dict(title='Title Settings', emojis=['🏷️', '⚙️', '✨'])))
                    proc.returncode = 0
                    return None, None
                proc.communicate = communicate
                return proc
            with mock.patch.object(titles.asyncio, 'create_subprocess_exec', side_effect=create):
                for model, reasoning in [('gpt-6.1-sol', 'high'), ('gpt-6-luna', 'xhigh')]:
                    titles.configure(model, reasoning)
                    value = await titles.generate('Task', context=[dict(role='user', text='Change title settings')])
                    self.assertEqual(value['model'], model)
                    self.assertEqual(value['reasoning'], reasoning)
                    self.assertEqual(requests[-1][requests[-1].index('--model') + 1], model)
                    self.assertIn(f'model_reasoning_effort="{reasoning}"', requests[-1])
            self.assertEqual(len(requests), 2)


if __name__ == '__main__':
    unittest.main()
