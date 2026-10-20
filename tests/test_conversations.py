import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_terminal_tldr as base
from test_terminal_tldr import NAME, TITLE
import conversations
import titles


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = conversations.Cadence(Path(self.temp.name))

    def test_first_task_then_tenth_new_user_message(self):
        first, due = self.store.observe('thread', ['0'], now=100)
        self.assertTrue(due)
        self.store.reviewed('thread', first['total'], now=100)
        for n in range(1, 10):
            _, due = self.store.observe('thread', [str(i) for i in range(n + 1)], now=101)
            self.assertFalse(due)
        _, due = self.store.observe('thread', [str(i) for i in range(11)], now=102)
        self.assertTrue(due)

    def test_hour_needs_new_user_activity_not_idle_polling(self):
        record, _ = self.store.observe('t', ['first'], now=100)
        self.store.reviewed('t', record['total'], now=100)
        self.assertFalse(self.store.observe('t', ['first'], now=8000)[1])
        self.assertTrue(self.store.observe('t', ['first', 'next'], now=8000)[1])

    def test_restart_does_not_recount_old_messages(self):
        record, _ = self.store.observe('t', ['first'], now=100)
        self.store.reviewed('t', record['total'], now=100)
        restarted = conversations.Cadence(self.store.directory)
        record, due = restarted.observe('t', ['first'], now=101)
        self.assertEqual(record['total'], 1)
        self.assertFalse(due)

    def test_agent_acknowledgement_resets_automatic_counter(self):
        self.store.observe('t', ['first'], now=100)
        record, due = self.store.observe('t', ['first', 'second'], now=120, acknowledged_at=119)
        self.assertFalse(due)
        self.assertEqual(record['reviewedTotal'], record['total'])

    def test_context_never_saved(self):
        self.store.observe('t', ['message-id'], now=100)
        self.assertEqual(set(self.store.get('t')), {'seen', 'total'})
        self.assertEqual(next(self.store.directory.iterdir()).stat().st_mode & 0o777, 0o600)


class MatchingTests(unittest.TestCase):
    def test_variation_selectors_and_spinner_do_not_break_mapping(self):
        target = dict(sid='s', native=conversations.normalized('⠋ 🖥 Terminal Titles | Working'))
        result = conversations.match_targets([target], [{'id': 't', 'name': '🖥️ Terminal Titles'}])
        self.assertEqual(result['s']['thread'], 't')

    def test_two_conversations_with_same_name_are_rejected(self):
        self.assertEqual(conversations.match_targets(
            [{'sid': 's', 'native': NAME}],
            [{'id': 'a', 'name': NAME}, {'id': 'b', 'name': NAME}]), {})

    def test_two_tabs_with_same_name_are_rejected(self):
        self.assertEqual(conversations.match_targets(
            [{'sid': 's', 'native': NAME}, {'sid': 'other', 'native': NAME}],
            [{'id': 'a', 'name': NAME}]), {})

    def test_initial_uuid_can_match_without_guessing_focus(self):
        tid = '01a15985-9495-7e62-9d50-d3f2a0b79007'
        result = conversations.match_targets(
            [{'sid': 's', 'native': conversations.normalized(tid + ' | Starting')}],
            [{'id': tid, 'name': None}])
        self.assertEqual(result['s']['thread'], tid)

class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.reader = conversations.Claude(self.home)
        self.tid = '7d2d7ab9-0afb-4517-a497-192d0af83beb'
        self.target = dict(pid=123, sid='s', owner='owner', tab='tab', native='Old task', provider='claude')
        titles.atomic_json(self.home / 'sessions/123.json', {
            'pid': 123, 'sessionId': self.tid, 'cwd': '/test/project', 'kind': 'interactive'})
        self.path = self.home / 'projects/-test-project' / (self.tid + '.jsonl')
        self.path.parent.mkdir(parents=True)
        self.path.write_text('')

    def test_exact_foreground_pid_maps_claude_without_codex(self):
        binding = self.reader.binding(self.target)
        self.assertEqual(binding['thread'], self.tid)
        self.assertEqual(binding['transcript'], self.path)

    def test_tool_results_and_meta_are_not_user_messages(self):
        events = [
            dict(sessionId=self.tid, uuid='user-1', type='user',
                 message={'content': [{'type': 'text', 'text': 'Build frequency visualization'}]}),
            dict(sessionId=self.tid, uuid='tool-result', type='user',
                 message={'content': [{'type': 'tool_result', 'content': 'SECRET TOOL OUTPUT'}]}),
            dict(sessionId=self.tid, uuid='meta', type='user', isMeta=True,
                 message={'content': 'INTERNAL CONTEXT'}),
            dict(sessionId=self.tid, uuid='answer', type='assistant',
                 message={'content': [{'type': 'text', 'text': 'Implementing frequency bands'}]}),
        ]
        self.path.write_text('\n'.join(json.dumps(e) for e in events))
        ids, context = self.reader.recent(self.reader.binding(self.target))
        self.assertEqual(ids, ['user-1'])
        self.assertEqual(len(context), 2)
        self.assertNotIn('SECRET', json.dumps(context))
        self.assertNotIn('INTERNAL', json.dumps(context))

    def test_wrong_pid_or_path_escape_is_rejected(self):
        self.assertIsNone(self.reader.binding({**self.target, 'pid': 456}))
        titles.atomic_json(self.home / 'sessions/123.json', {
            'pid': 123, 'sessionId': '../outside', 'cwd': '/test/project', 'kind': 'interactive'})
        self.assertIsNone(self.reader.binding(self.target))

    def test_idle_file_uses_memory_cache_and_context_is_bounded(self):
        self.path.write_text(json.dumps(dict(sessionId=self.tid, uuid='u', type='user',
                                            message={'content': 'a' * 10000})))
        binding = self.reader.binding(self.target)
        first = self.reader.recent(binding)
        with mock.patch.object(Path, 'open', side_effect=AssertionError('idle file was reread')):
            self.assertEqual(self.reader.recent(binding), first)
        self.assertLessEqual(sum(len(m['text']) for m in first[1]), 6000)


class AutomaticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base.ServiceTests.setUp(self)
        self.ids = ['message-0']
        self.context = [{'role': 'user', 'text': 'Build keyboard autocorrect'}]
        owner = self
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def threads(self): return [{'id': 'thread', 'name': NAME}]
            async def recent(self, _): return list(owner.ids), owner.context
        self.auto = conversations.AutomaticTitles(
            self.svc, conversations.Cadence(Path(self.directory.name) / 'cadence'), Client)
        self.svc.automatic = self.auto

    async def drain(self):
        task = asyncio.create_task(self.auto.worker())
        try:
            await asyncio.wait_for(self.auto.queue.join(), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_automatic_review_changes_real_writer_without_hooks(self):
        await self.auto.scan()
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock, return_value=TITLE) as generate:
            await self.drain()
        self.tab.async_set_title.assert_awaited_once_with(titles.format_title(TITLE))
        self.assertEqual(generate.call_args.kwargs['context'], self.context)
        self.assertEqual(self.auto.cadence.get('thread')['reviewedTotal'], 1)

    async def test_manual_refresh_reads_current_context_even_when_not_due(self):
        await self.auto.scan()
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock, return_value=TITLE):
            await self.drain()
        self.context = [{'role': 'user', 'text': 'Completely different task now'}]
        await self.auto.scan(manual=True)
        other = {**TITLE, 'title': 'Different Current Task'}
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock, return_value=other) as generate:
            await self.drain()
        self.assertEqual(generate.call_args.kwargs['context'], self.context)
        self.assertEqual(self.state.override, titles.format_title(other))
        self.assertEqual(self.auto.refresh_changed, 1)

    async def test_idle_scans_do_not_generate_again(self):
        await self.auto.scan()
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock, return_value=TITLE):
            await self.drain()
        for _ in range(15):
            await self.auto.scan()
        self.assertTrue(self.auto.queue.empty())

    async def test_tenth_message_enqueues_a_review_automatically(self):
        await self.auto.scan()
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock, return_value=TITLE):
            await self.drain()
        for n in range(1, 10):
            self.ids.append(f'message-{n}')
            await self.auto.scan()
            self.assertTrue(self.auto.queue.empty())
        self.ids.append('message-10')
        await self.auto.scan()
        self.assertEqual(self.auto.queue.qsize(), 1)

    async def test_manual_pin_during_model_request_is_not_overwritten(self):
        await self.auto.scan(manual=True)
        async def generate(*args, **kwargs):
            self.state.override = 'User pinned this'
            return TITLE
        with mock.patch.object(titles, 'generate', side_effect=generate):
            await self.drain()
        self.tab.async_set_title.assert_not_awaited()
        self.assertEqual(self.auto.refresh_skipped, 1)

    async def test_changed_foreground_job_discards_queued_review(self):
        await self.auto.scan()
        self.item.values['jobPid'] = 456
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock) as generate:
            await self.drain()
            generate.assert_not_awaited()

    async def test_old_shared_terminal_environment_is_not_used(self):
        await self.auto.scan()
        result = await self.svc.control(dict(action='prompt', terminal='wrong-shared-terminal',
                                            thread='thread', turn='current', provider='codex'))
        self.assertTrue(result['ok'])
        result = await self.svc.control(dict(action='set', terminal='wrong-shared-terminal',
                                            thread='thread', value=TITLE, provider='codex'))
        self.assertTrue(result['ok'])

    async def test_unknown_thread_cannot_use_stale_terminal_environment(self):
        await self.auto.scan()
        result = await self.svc.control(dict(action='set', terminal='session-1',
                                            thread='child', value=TITLE, provider='codex'))
        self.assertFalse(result['ok'])

    async def test_failure_is_reported_not_counted_as_updated(self):
        await self.auto.scan(manual=True)
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock, side_effect=RuntimeError('offline')):
            with self.assertLogs('terminal-tldr', level='WARNING'):
                await self.drain()
        self.assertEqual(self.auto.refresh_failed, 1)
        self.assertEqual(self.auto.refresh_done, 0)
        self.tab.async_set_title.assert_not_awaited()
        await self.auto.scan()
        self.assertTrue(self.auto.queue.empty())


if __name__ == '__main__':
    unittest.main()
