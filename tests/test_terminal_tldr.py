from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import titles
spec = importlib.util.spec_from_file_location('service', ROOT / 'iterm2/terminal_tldr_iterm.py')
service = importlib.util.module_from_spec(spec)
sys.modules['service'] = service
with mock.patch.dict(sys.modules, {'iterm2': mock.Mock()}):
    spec.loader.exec_module(service)

TITLE = {'emojis': ['⌨️', '🔧', '✨'], 'title': 'Keyboard Autocorrect'}
NAME = 'iOS keyboard extension autocorrect model'


class NativeNames(unittest.TestCase):
    def test_spinner_and_status_are_not_semantic_changes(self):
        state = service.Session(values={'autoName': 'Fix tab titles | Ready'})
        for spinner in ('⠴', '⠋', '⠙', '◐', '◑', '✳', '✻'):
            self.assertFalse(state.update('autoName', spinner + ' Fix tab titles | Working'))
        self.assertTrue(state.update('autoName', 'Build keyboard onboarding | Ready'))

    def test_default_and_uuid_names_are_not_sent_to_model(self):
        for value in ('', 'codex', 'Claude Code', 'new session',
                      '01a11195-26bb-7661-bdaa-83fc3e51e206 | Ready',
                      '01a11195-26bb-7661-bdaa-83fc3e51e206 | Starting',
                      '01a11195-26bb-7661-bdaa-83fc3e51e206 | Future Status'):
            self.assertEqual(titles.native_title(value), '')

    def test_blinking_action_required_is_not_a_new_task(self):
        state = service.Session(values={'autoName': 'Investigate note-sharing tool | Ready'})
        for marker in ('.', '!'):
            self.assertFalse(state.update('autoName', f'[ {marker} ] Action Required | Investigate note-sharing tool'))

    def test_native_name_is_bounded(self):
        self.assertEqual(titles.native_title('x' * 241), '')

    def test_profile_name_is_not_a_task(self):
        for profile in ('Default', 'My Custom Profile'):
            state = service.Session(values={'jobName': 'codex', 'autoName': profile, 'profileName': profile})
            self.assertEqual(state.name, '')

    def test_only_agent_jobs_qualify(self):
        self.assertTrue(titles.agent_job('codex', 'codex --yolo'))
        self.assertTrue(titles.agent_job('2.1.291', 'claude --resume'))
        self.assertFalse(titles.agent_job('zsh', 'codex --yolo'))
        self.assertFalse(titles.agent_job('rg', 'rg codex'))
        self.assertFalse(titles.agent_job('2.1.291', 'echo claude'))

    def test_generated_title_validation(self):
        self.assertEqual(titles.format_title(TITLE), '⌨️ 🔧 ✨ Keyboard Autocorrect')
        self.assertEqual(titles.format_title({'emojis': ['a', 'b', 'c'], 'title': 'No'}), '')
        self.assertEqual(titles.format_title({'emojis': ['🔧'] * 3, 'title': 'No'}), '')
        self.assertEqual(titles.format_title({'emojis': ['🔧', '✨', '\x1b🔎'], 'title': 'No'}), '')
        self.assertEqual(titles.format_title({**TITLE, 'title': 'One Two Three Four'}), '⌨️ 🔧 ✨ One Two Three')
        self.assertEqual(titles.format_title({**TITLE, 'title': r'Unsafe \(expression)'}), '⌨️ 🔧 ✨ Unsafe expression')


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = titles.Cache(Path(self.directory.name))

    def test_disk_read_happens_once_per_name_not_per_lookup(self):
        for _ in range(100):
            self.cache.get(NAME)
        self.assertEqual(self.cache.reads, 1)
        self.assertEqual(self.cache.writes, 0)

    def test_restart_reuses_small_individual_cache(self):
        self.cache.save(NAME, TITLE)
        other = titles.Cache(self.cache.directory)
        self.assertEqual(titles.format_title(other.get(NAME)), titles.format_title(TITLE))
        self.assertEqual(len(list(self.cache.directory.iterdir())), 1)
        self.assertLess(next(self.cache.directory.iterdir()).stat().st_size, 1024)

    def test_cache_is_private_and_atomic(self):
        self.cache.save(NAME, TITLE)
        path = next(self.cache.directory.iterdir())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.cache.directory.glob('.title-*')))

    def test_large_or_corrupt_cache_is_rejected(self):
        path = self.cache.directory / 'bad.json'
        path.write_bytes(b' ' * (titles.MAX_FILE + 1))
        self.assertEqual(titles.read_json(path), {})
        path.write_text('not JSON')
        self.assertEqual(titles.read_json(path), {})

    def test_memory_cache_is_bounded(self):
        for n in range(600):
            self.cache.get(str(n))
        self.assertEqual(len(self.cache.records), 512)

    def test_disk_full_does_not_cause_repeated_generation(self):
        with mock.patch.object(titles, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertLogs('terminal-tldr', level='ERROR'):
                self.cache.save(NAME, TITLE)
        self.assertEqual(titles.format_title(self.cache.get(NAME)), titles.format_title(TITLE))
        self.assertFalse(self.cache.due(NAME))

    def test_disk_full_retains_failure_backoff_in_memory(self):
        with mock.patch.object(titles, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertLogs('terminal-tldr', level='ERROR'):
                self.cache.failed(NAME)
        self.assertFalse(self.cache.due(NAME))

    def test_failure_backoff_and_attempt_cap_survive_restart(self):
        with mock.patch.object(titles.time, 'time', return_value=1000):
            self.assertTrue(self.cache.due(NAME))
            first = self.cache.failed(NAME)
            self.assertEqual(first['retryAt'], 1300)
            self.assertFalse(self.cache.due(NAME))
        other = titles.Cache(self.cache.directory)
        with mock.patch.object(titles.time, 'time', return_value=1301):
            self.assertTrue(other.due(NAME))
            self.assertEqual(other.failed(NAME)['retryAt'], 1901)
        with mock.patch.object(titles.time, 'time', return_value=2000):
            other.failed(NAME)
        with mock.patch.object(titles.time, 'time', return_value=9999999):
            self.assertFalse(titles.Cache(self.cache.directory).due(NAME))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = titles.Cache(Path(self.directory.name))
        self.session = SimpleNamespace(session_id='session-1')
        self.tab = SimpleNamespace(tab_id='tab-1', all_sessions=[self.session], current_session=self.session,
                                   async_set_title=mock.AsyncMock(), async_set_variable=mock.AsyncMock())
        self.app = SimpleNamespace(windows=[SimpleNamespace(tabs=[self.tab])])
        self.svc = service.Service(self.app, self.cache)
        self.item = service.Session(values={'jobPid': 123, 'jobName': 'codex', 'commandLine': 'codex', 'autoName': NAME, 'profileName': 'Default'}, task=mock.Mock())
        self.state = service.Tab(initialized=True, task=mock.Mock())
        self.svc.sessions[self.session.session_id] = self.item
        self.svc.tabs[self.tab.tab_id] = self.state
        self.addCleanup(lambda: [timer.cancel() for timer in self.svc.timers.values()])

    async def test_cached_title_then_no_idle_work_or_api_writes(self):
        self.cache.save(NAME, TITLE)
        await self.svc.reconcile()
        self.assertEqual(self.tab.async_set_title.await_count, 1)
        self.assertEqual(self.tab.async_set_variable.await_count, 2)
        reads, writes = self.cache.reads, self.cache.writes
        for _ in range(100):
            await self.svc.reconcile()
        self.assertEqual(self.tab.async_set_title.await_count, 1)
        self.assertEqual(self.tab.async_set_variable.await_count, 2)
        self.assertEqual((self.cache.reads, self.cache.writes), (reads, writes))
        self.assertEqual(self.svc.queue.qsize(), 0)

    async def test_event_loop_does_not_poll(self):
        with mock.patch.object(self.svc, 'reconcile', new_callable=mock.AsyncMock) as reconcile:
            task = asyncio.create_task(self.svc.update_loop())
            await asyncio.sleep(0.03)
            reconcile.assert_not_awaited()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_uncached_name_is_queued_only_once(self):
        for _ in range(20):
            await self.svc.reconcile()
        self.assertEqual(self.svc.queue.qsize(), 1)
        self.tab.async_set_title.assert_not_awaited()  # Native title stays visible.

    async def test_existing_manual_rename_is_never_claimed(self):
        self.state.override = 'My manual title'
        self.cache.save(NAME, TITLE)
        await self.svc.reconcile()
        self.tab.async_set_title.assert_not_awaited()
        self.tab.async_set_variable.assert_not_awaited()

    async def test_manual_override_survives_native_name_changes(self):
        self.state.owner = self.item.owner(self.session.session_id)
        self.state.manual_owner = self.state.owner
        self.state.override = 'Mine'
        self.item.values['autoName'] = 'Some different task'
        await self.svc.reconcile()
        self.tab.async_set_title.assert_not_awaited()
        self.assertEqual(self.svc.queue.qsize(), 0)

    async def test_new_foreground_job_can_claim_old_manual_title(self):
        self.state.owner = 'v2:session-1:122'
        self.state.manual_owner = self.state.owner
        self.state.override = 'Old manual title'
        self.cache.save(NAME, TITLE)
        await self.svc.reconcile()
        self.tab.async_set_title.assert_awaited_once_with(titles.format_title(TITLE))

    async def test_new_name_does_not_inherit_old_managed_title(self):
        self.state.managed = self.state.override = 'Old title'
        self.state.owner = self.item.owner(self.session.session_id)
        await self.svc.reconcile()
        self.tab.async_set_title.assert_awaited_once_with('')
        self.assertEqual(self.svc.queue.qsize(), 1)

    async def test_shell_clears_only_our_old_title(self):
        self.state.managed = self.state.override = 'Old title'
        self.state.owner = 'v2:session-1:122'
        self.item.values.update(jobName='zsh', jobPid=124)
        await self.svc.reconcile()
        self.tab.async_set_title.assert_awaited_once_with('')
        self.assertEqual(self.svc.queue.qsize(), 0)

    async def test_closed_tabs_drop_queued_work_without_generation(self):
        await self.svc.reconcile()
        self.app.windows = []
        await self.svc.reconcile()
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock) as generate:
            task = asyncio.create_task(self.svc.worker())
            await self.svc.queue.join()
            generate.assert_not_awaited()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.item.task.cancel.assert_called_once()
        self.state.task.cancel.assert_called_once()

    async def test_failed_model_is_not_retried_in_a_loop(self):
        await self.svc.reconcile()
        with mock.patch.object(titles, 'generate', new_callable=mock.AsyncMock, side_effect=RuntimeError('offline')) as generate:
            task = asyncio.create_task(self.svc.worker())
            await self.svc.queue.join()
            for _ in range(20):
                await self.svc.reconcile()
            self.assertEqual(generate.await_count, 1)
            self.assertEqual(self.svc.queue.qsize(), 0)
            self.assertEqual(len(self.svc.timers), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_inflight_result_does_not_rename_reused_tab(self):
        await self.svc.reconcile()
        started, finish = asyncio.Event(), asyncio.Event()
        async def generate(name):
            started.set()
            await finish.wait()
            return TITLE
        with mock.patch.object(titles, 'generate', side_effect=generate):
            task = asyncio.create_task(self.svc.worker())
            await started.wait()
            self.item.values.update(jobPid=124, autoName='Different task')
            await self.svc.reconcile()
            finish.set()
            while self.svc.current_request:
                await asyncio.sleep(0)
            await self.svc.reconcile()
            self.tab.async_set_title.assert_not_awaited()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_duplicate_names_share_single_model_request(self):
        second = SimpleNamespace(tab_id='tab-2', all_sessions=[self.session], current_session=self.session,
                                 async_set_title=mock.AsyncMock(), async_set_variable=mock.AsyncMock())
        self.app.windows[0].tabs.append(second)
        self.svc.tabs['tab-2'] = service.Tab(initialized=True, task=mock.Mock())
        await self.svc.reconcile()
        self.assertEqual(self.svc.queue.qsize(), 1)


class ArchitectureTests(unittest.TestCase):
    def test_retired_paths_are_not_in_runtime(self):
        core = (ROOT / 'lib/titles.py').read_text()
        daemon = (ROOT / 'iterm2/terminal_tldr_iterm.py').read_text()
        for banned in ('state.json', 'agent-sessions.md', 'rglob(', 'git_branch(', 'run_cli(', 'read_jsonl(', 'lsof'):
            self.assertNotIn(banned, core + daemon)
        self.assertEqual(core.count('create_subprocess_exec('), 1)
        self.assertNotIn('subprocess', daemon)
        for banned in ('async_close', 'async_send_text', 'async_create', 'async_split', 'set_badge'):
            self.assertNotIn(banned, daemon)


class GeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_success_uses_bounded_result_and_no_terminal_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = SimpleNamespace(pid=987654, returncode=None)
            async def create(*args, **kwargs):
                self.assertTrue(kwargs['start_new_session'])
                self.assertIn('shell_tool', args)
                self.assertIn('unified_exec', args)
                self.assertIn('web_search="disabled"', args)
                self.assertNotIn('TERM_SESSION_ID', kwargs['env'])
                output = Path(args[args.index('--output-last-message') + 1])
                async def communicate(prompt):
                    self.assertIn(NAME.encode(), prompt)
                    output.write_text(json.dumps(TITLE))
                    proc.returncode = 0
                    return None, None
                proc.communicate = communicate
                return proc
            with mock.patch.object(titles, 'APP_DIR', Path(directory)), mock.patch.object(
                titles.asyncio, 'create_subprocess_exec', side_effect=create
            ), mock.patch.dict(titles.os.environ, {'TERM_SESSION_ID': 'interactive-terminal'}):
                result = await titles.generate(NAME)
            self.assertEqual(titles.format_title(result), titles.format_title(TITLE))
            self.assertNotIn(proc.pid, titles.CHILDREN)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_timeout_kills_only_own_child_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = SimpleNamespace(pid=987655, returncode=None,
                                   communicate=mock.AsyncMock(side_effect=asyncio.TimeoutError()))
            async def wait():
                proc.returncode = -9
            proc.wait = mock.AsyncMock(side_effect=wait)
            with mock.patch.object(titles, 'APP_DIR', Path(directory)), mock.patch.object(
                titles.asyncio, 'create_subprocess_exec', new_callable=mock.AsyncMock, return_value=proc
            ), mock.patch.object(titles.os, 'killpg') as kill, mock.patch.object(
                titles.asyncio, 'wait_for', wraps=asyncio.wait_for
            ) as deadline:
                with self.assertRaises(asyncio.TimeoutError):
                    await titles.generate(NAME)
            self.assertEqual(deadline.call_args.kwargs['timeout'], 10)
            kill.assert_called_once_with(proc.pid, titles.signal.SIGKILL)
            proc.wait.assert_awaited_once()
            self.assertNotIn(proc.pid, titles.CHILDREN)

    async def test_cancelled_request_reaps_own_process(self):
        with tempfile.TemporaryDirectory() as directory:
            started = asyncio.Event()
            proc = SimpleNamespace(pid=987656, returncode=None)
            async def communicate(_):
                started.set()
                await asyncio.Event().wait()
            async def wait():
                proc.returncode = -9
            proc.communicate, proc.wait = communicate, mock.AsyncMock(side_effect=wait)
            with mock.patch.object(titles, 'APP_DIR', Path(directory)), mock.patch.object(
                titles.asyncio, 'create_subprocess_exec', new_callable=mock.AsyncMock, return_value=proc
            ), mock.patch.object(titles.os, 'killpg') as kill:
                task = asyncio.create_task(titles.generate(NAME))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            kill.assert_called_once_with(proc.pid, titles.signal.SIGKILL)
            proc.wait.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
