from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import agent_titles
import setup
import titles
import test_terminal_tldr as base
from test_terminal_tldr import TITLE, NAME

loader = importlib.machinery.SourceFileLoader('cli', str(ROOT / 'bin/terminal-tldr'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = agent_titles.AgentTitles(Path(self.temp.name))

    def test_first_prompt_then_ten_user_messages(self):
        self.assertTrue(self.store.prompt('s', 'owner', 'thread', '0', now=100)['due'])
        self.store.review('s', 'owner', 'thread', TITLE, now=100)
        for n in range(1, 10):
            self.assertFalse(self.store.prompt('s', 'owner', 'thread', str(n), now=101)['due'])
        self.assertTrue(self.store.prompt('s', 'owner', 'thread', '10', now=101)['due'])

    def test_hour_triggers_only_when_next_prompt_arrives(self):
        self.store.prompt('s', 'o', 't', '0', now=100)
        self.store.review('s', 'o', 't', TITLE, now=100)
        self.assertFalse(self.store.prompt('s', 'o', 't', '1', now=3699)['due'])
        self.assertTrue(self.store.prompt('s', 'o', 't', '2', now=3700)['due'])

    def test_review_without_change_resets_both_thresholds(self):
        self.store.prompt('s', 'o', 't', '0', now=100)
        self.store.review('s', 'o', 't', TITLE, now=100)
        self.store.prompt('s', 'o', 't', '1', now=3700)
        self.store.review('s', 'o', 't', now=3700)
        self.assertFalse(self.store.prompt('s', 'o', 't', '2', now=3701)['due'])
        self.assertEqual(self.store.get('s', 'o')['title'], TITLE)

    def test_duplicate_turn_not_counted_twice(self):
        for _ in range(20):
            self.store.prompt('s', 'o', 't', 'same', now=100)
        self.assertEqual(self.store.get('s', 'o')['prompts'], 1)

    def test_restart_preserves_review(self):
        self.store.prompt('s', 'o', 't', '0', now=100)
        self.store.review('s', 'o', 't', TITLE, now=100)
        restarted = agent_titles.AgentTitles(self.store.directory)
        self.assertFalse(restarted.prompt('s', 'o', 't', '1', now=101)['due'])

    def test_foreground_replacement_rejects_old_writer(self):
        self.store.prompt('s', 'old-pid', 't', '0', now=100)
        self.assertFalse(self.store.review('s', 'new-pid', 't', TITLE)['ok'])

    def test_other_thread_cannot_rename(self):
        self.store.prompt('s', 'o', 'parent', '0', now=100)
        self.assertFalse(self.store.review('s', 'o', 'child', TITLE)['ok'])

    def test_new_conversation_resets_title_and_cadence(self):
        self.store.prompt('s', 'o', 'first', '0', now=100)
        self.store.review('s', 'o', 'first', TITLE, now=100)
        result = self.store.prompt('s', 'o', 'second', '0', now=101)
        self.assertTrue(result['initial'])
        self.assertTrue(result['due'])
        self.assertEqual(result['title'], '')

    def test_only_bounded_title_fields_are_saved(self):
        self.store.prompt('s', 'o', 't', '0', now=100)
        self.store.review('s', 'o', 't', {**TITLE, 'prompt': 'Never store me'}, now=100)
        self.assertNotIn('Never store me', self.store.path('s').read_text())
        self.assertEqual(self.store.path('s').stat().st_mode & 0o777, 0o600)

    def test_invalid_title_does_not_reset_review(self):
        self.store.prompt('s', 'o', 't', '0', now=100)
        self.assertFalse(self.store.review('s', 'o', 't', {'title': 'bad'})['ok'])
        self.assertEqual(self.store.get('s', 'o')['reviewedAt'], 0)

    def test_terminal_identity_never_uses_focus(self):
        self.assertEqual(agent_titles.terminal_id({}), '')
        self.assertEqual(agent_titles.terminal_id({'ITERM_SESSION_ID': 'w0t0p0:abc-123'}), 'abc-123')
        self.assertEqual(agent_titles.terminal_id({'ITERM_SESSION_ID': '../bad'}), '')


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.ServiceTests.setUp
    async def register(self):
        return await self.svc.control(dict(action='prompt', terminal='session-1', thread='thread', turn='1'))

    async def set_title(self):
        return await self.svc.control(dict(action='set', terminal='session-1', thread='thread', value=TITLE))

    async def test_agent_title_wins_over_stale_native_cache_without_model(self):
        await self.register()
        self.assertTrue((await self.set_title())['ok'])
        await self.svc.reconcile()
        self.tab.async_set_title.assert_awaited_once_with(titles.format_title(TITLE))
        self.assertEqual(self.svc.queue.qsize(), 0)
        self.item.values['autoName'] = 'Stale upstream name changed'
        await self.svc.reconcile()
        self.assertEqual(self.tab.async_set_title.await_count, 1)

    async def test_agent_review_without_change_causes_no_title_write(self):
        await self.register()
        await self.set_title()
        await self.svc.reconcile()
        result = await self.svc.control(dict(action='review', terminal='session-1', thread='thread'))
        self.assertTrue(result['ok'])
        await self.svc.reconcile()
        self.assertEqual(self.tab.async_set_title.await_count, 1)

    async def test_manual_pin_blocks_agent_and_reminder(self):
        self.state.override = 'My chosen title'
        result = await self.register()
        self.assertTrue(result['manual'])
        self.assertFalse(result['ok'])
        self.tab.async_set_title.assert_not_awaited()

    async def test_unknown_terminal_and_invalid_protocol(self):
        for request in ([], {}, {'action': 'set'}, {'action': 'set', 'thread': 't', 'terminal': 'unknown'}):
            self.assertFalse((await self.svc.control(request))['ok'])

    async def test_set_rejects_other_thread(self):
        await self.register()
        result = await self.svc.control(dict(action='set', terminal='session-1', thread='child', value=TITLE))
        self.assertFalse(result['ok'])

    async def test_set_rejects_replaced_foreground_job(self):
        await self.register()
        self.item.values['jobPid'] = 999
        self.assertFalse((await self.set_title())['ok'])

    async def test_refresh_requeues_native_title_and_is_rate_limited(self):
        self.cache.save(NAME, TITLE)
        await self.svc.reconcile()
        result = await self.svc.control({'action': 'refresh'})
        self.assertEqual(result['nativeTitles'], 1)
        await self.svc.reconcile()
        self.assertEqual(self.svc.queue.qsize(), 1)
        repeated = await self.svc.control({'action': 'refresh'})
        self.assertIn('Already requested', repeated['message'])
        self.assertEqual(self.svc.queue.qsize(), 1)

    async def test_refresh_requests_agent_review_without_regenerating(self):
        await self.register()
        await self.set_title()
        await self.svc.reconcile()
        result = await self.svc.control({'action': 'refresh'})
        self.assertEqual(result['agentReviews'], 1)
        self.assertEqual(result['nativeTitles'], 0)
        await self.svc.reconcile()
        self.assertEqual(self.tab.async_set_title.await_count, 1)
        self.assertEqual(self.svc.queue.qsize(), 0)
        prompt = await self.svc.control(dict(action='prompt', terminal='session-1', thread='thread', turn='2'))
        self.assertTrue(prompt['due'])

    async def test_refresh_does_not_touch_manual_titles(self):
        self.state.override = 'Pinned title'
        self.cache.save(NAME, TITLE)
        result = await self.svc.control({'action': 'refresh'})
        self.assertEqual(result['manualTitles'], 1)
        self.assertEqual(result['nativeTitles'], 0)
        self.assertEqual(self.cache.get(NAME), TITLE)
        self.tab.async_set_title.assert_not_awaited()

    async def test_refresh_reviews_agent_even_without_native_task_name(self):
        await self.register()
        await self.set_title()
        self.item.values['autoName'] = ''
        result = await self.svc.control({'action': 'refresh'})
        self.assertEqual(result['agentReviews'], 1)
        self.assertEqual(result['nativeTitles'], 0)


class HookInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / '.claude/settings.json'
        self.path.parent.mkdir()
        self.original = {'permissions': {'allow': ['existing']}, 'hooks': {
            'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'existing-logger'}]}]}}
        self.path.write_text(json.dumps(self.original))
        self.patch = mock.patch.object(titles, 'APP_DIR', self.home / '.terminal-tldr')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_install_is_idempotent_and_keeps_existing_hooks(self):
        setup.edit_hooks(self.path, True, self.home)
        first = self.path.read_text()
        setup.edit_hooks(self.path, True, self.home)
        self.assertEqual(first, self.path.read_text())
        value = json.loads(first)
        self.assertEqual(value['permissions'], self.original['permissions'])
        self.assertEqual(value['hooks']['UserPromptSubmit'][0], self.original['hooks']['UserPromptSubmit'][0])
        self.assertEqual(len(value['hooks']['UserPromptSubmit']), 2)

    def test_uninstall_removes_only_our_handler(self):
        setup.edit_hooks(self.path, True, self.home)
        setup.edit_hooks(self.path, False, self.home)
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_malformed_configuration_is_not_overwritten(self):
        self.path.write_text('broken')
        with self.assertRaises(ValueError):
            setup.edit_hooks(self.path, True, self.home)
        self.assertEqual(self.path.read_text(), 'broken')

    def test_pause_waits_for_old_launchd_job_to_disappear(self):
        results = [SimpleNamespace(returncode=n) for n in (0, 0, 0, 0, 113)]
        with mock.patch.object(setup, 'launchctl', side_effect=results) as launch, \
             mock.patch.object(setup.time, 'sleep') as sleep:
            setup.pause()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([c.args[0] for c in launch.call_args_list],
                         ['disable', 'bootout', 'print', 'print', 'print'])

    def test_resume_does_not_restart_an_already_loaded_helper(self):
        with mock.patch.object(Path, 'exists', return_value=True), \
             mock.patch.object(setup, 'launchctl', return_value=SimpleNamespace(returncode=0)) as launch:
            setup.resume()
        self.assertEqual([c.args[0] for c in launch.call_args_list], ['enable', 'print'])


class HookTests(unittest.TestCase):
    def call_hook(self, payload, response=None, env=None):
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        out = io.StringIO()
        with mock.patch.object(sys, 'stdin', stream), mock.patch.object(sys, 'stdout', out), \
             mock.patch.dict(os.environ, env or {'ITERM_SESSION_ID': 'w0t0p0:session-1'}, clear=True), \
             mock.patch.object(agent_titles, 'request', return_value=response or {'ok': True, 'due': True}) as request:
            cli.agent_hook()
        return out.getvalue(), request

    def test_due_context_does_not_forward_user_prompt(self):
        out, request = self.call_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 't', 'prompt': 'PRIVATE USER TEXT'})
        self.assertEqual(json.loads(out)['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertNotIn('PRIVATE USER TEXT', str(request.call_args))
        self.assertNotIn('PRIVATE USER TEXT', out)

    def test_no_reminder_when_not_due(self):
        out, _ = self.call_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 't'}, {'ok': True, 'due': False})
        self.assertEqual(out, '')

    def test_subagent_and_fork_are_ignored(self):
        for extra in ({'agent_id': 'child'}, {'source': 'fork'}):
            out, request = self.call_hook({'hook_event_name': 'SessionStart', 'session_id': 't', **extra})
            self.assertEqual(out, '')
            request.assert_not_called()

    def test_inherited_terminal_different_thread_is_ignored(self):
        out, request = self.call_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 'child'},
                                     env={'ITERM_SESSION_ID': 'w0t0p0:s', 'CODEX_THREAD_ID': 'parent'})
        self.assertEqual(out, '')
        request.assert_not_called()

    def test_service_failure_never_blocks_agent(self):
        with mock.patch.object(agent_titles, 'request', side_effect=OSError('unavailable')), \
             mock.patch.dict(os.environ, {'ITERM_SESSION_ID': 'w0t0p0:s'}, clear=True), \
             mock.patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(
                 b'{"hook_event_name":"UserPromptSubmit","session_id":"t"}'))), \
             mock.patch.object(sys, 'stdout', io.StringIO()) as output:
            cli.agent_hook()
            self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
