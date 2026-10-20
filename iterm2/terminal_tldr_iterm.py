#!/usr/bin/env python3
"""Event-driven decoration of native task names. No history scanning or polling."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import fcntl
import json
import logging
import os
from pathlib import Path
import resource
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import titles
import agent_titles
import conversations
import iterm2

LOG = logging.getLogger('terminal-tldr')
SOCKET = titles.APP_DIR / 'status.sock'


@dataclass
class Session:
    values: dict = field(default_factory=dict)
    task: asyncio.Task | None = None

    def update(self, name, value):
        previous = self.values.get(name)
        if name == 'autoName' and titles.native_title(value) == titles.native_title(previous):
            return False
        if value == previous:
            return False
        self.values[name] = value
        return True

    @property
    def name(self):
        if not titles.agent_job(str(self.values.get('jobName') or ''), str(self.values.get('commandLine') or '')):
            return ''
        name = titles.native_title(self.values.get('autoName') or '')
        return '' if name == self.values.get('profileName') else name

    def owner(self, sid):
        return f'v2:{sid}:{self.values.get("jobPid", "")}'


@dataclass
class Tab:
    override: str = ''
    managed: str = ''
    owner: str = ''
    manual_owner: str = ''
    task: asyncio.Task | None = None
    initialized: bool = False


class Service:
    def __init__(self, app, cache=None, agent_store=None, automatic=False):
        self.app = app
        self.cache = cache or titles.Cache()
        self.agents = agent_store or agent_titles.AgentTitles()
        self.mutation_lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()
        self.sessions: dict[str, Session] = {}
        self.tabs: dict[str, Tab] = {}
        self.dirty = asyncio.Event()
        self.queue = asyncio.Queue(maxsize=64)
        self.pending: set[str] = set()
        self.wanted: set[str] = set()
        self.timers = {}
        self.current_request = ''
        self.started = time.time()
        self.last_refresh = -60.0
        self.stats = dict(reconciles=0, nativeChanges=0, titleWrites=0, variableWrites=0,
                          modelCalls=0, modelFailures=0, transcriptBytesRead=0, gitCalls=0,
                          cliLookupProcesses=0, agentReviews=0, agentUpdates=0, conversationReviews=0)
        self.automatic = conversations.AutomaticTitles(self) if automatic else None

    def conversation_targets(self):
        result = []
        for window in self.app.windows:
            for tab in window.tabs:
                session, state = tab.current_session, self.tabs.get(tab.tab_id)
                if session is None or state is None or not state.initialized:
                    continue
                sid = session.session_id
                item = self.sessions.get(sid)
                if item is None or not item.values.get('jobPid') or not titles.agent_job(
                    str(item.values.get('jobName', '')), str(item.values.get('commandLine', ''))):
                    continue
                target = dict(sid=sid, owner=item.owner(sid), tab=tab.tab_id,
                              pid=item.values['jobPid'],
                              provider='codex' if item.values.get('jobName') == 'codex' else 'claude',
                              native=conversations.normalized(item.values.get('autoName', '')))
                if self.target_valid(target):
                    result.append(target)
        return result

    def target_valid(self, target):
        sid = target['sid']
        item, state = self.sessions.get(sid), self.tabs.get(target['tab'])
        if not item or not state or item.owner(sid) != target['owner']:
            return False
        if (not titles.agent_job(str(item.values.get('jobName', '')), str(item.values.get('commandLine', '')))
            or conversations.normalized(item.values.get('autoName', '')) != target['native']):
            return False
        if (state.manual_owner in (target['owner'], 'unknown')
            or (state.override not in ('', ' ', state.managed)
                and (not state.owner or state.owner == target['owner']))):
            return False
        return any(tab.tab_id == target['tab'] and tab.current_session is not None
                   and tab.current_session.session_id == sid for w in self.app.windows for tab in w.tabs)

    def task(self, coro):
        task = asyncio.create_task(coro)
        def done(t):
            if not t.cancelled() and t.exception():
                LOG.error('Service task failed', exc_info=t.exception())
                titles.stop_children()
                os._exit(1)  # launchd reconnects; never leave a half-dead updater.
        task.add_done_callback(done)
        return task

    async def watch_session(self, sid, state):
        async def watch(name):
            async with iterm2.VariableMonitor(self.app.connection, iterm2.VariableScopes.SESSION, name, sid) as monitor:
                session = self.app.get_session_by_id(sid)
                if session is None:
                    return
                state.values[name] = await session.async_get_variable(name)
                self.dirty.set()
                while True:
                    value = await monitor.async_get()
                    if not state.update(name, value):
                        continue
                    self.stats['nativeChanges'] += 1
                    self.dirty.set()
        await asyncio.gather(*(watch(name) for name in ('autoName', 'jobPid', 'jobName', 'commandLine', 'profileName')))

    async def watch_tab(self, tab, state):
        async with iterm2.VariableMonitor(self.app.connection, iterm2.VariableScopes.TAB, 'titleOverride', tab.tab_id) as monitor:
            state.override = await tab.async_get_variable('titleOverride') or ''
            state.managed = await tab.async_get_variable('user.terminalTldrManagedTitle') or ''
            state.owner = await tab.async_get_variable('user.terminalTldrOwner') or ''
            state.initialized = True
            self.dirty.set()
            while True:
                value = await monitor.async_get() or ''
                if value == state.override:
                    continue
                state.override = value
                state.manual_owner = state.owner or 'unknown'
                self.dirty.set()

    async def write(self, tab, state, owner, desired):
        if state.override != desired:
            state.override = desired  # Set before awaiting so our notification is not a manual rename.
            await tab.async_set_title(desired)
            self.stats['titleWrites'] += 1
        for variable, attribute, value in (
            ('user.terminalTldrManagedTitle', 'managed', desired),
            ('user.terminalTldrOwner', 'owner', owner),
        ):
            if getattr(state, attribute) != value:
                setattr(state, attribute, value)
                await tab.async_set_variable(variable, value)
                self.stats['variableWrites'] += 1

    def retry_later(self, name, record):
        if record.get('attempts', 0) >= titles.MAX_ATTEMPTS or name in self.timers:
            return
        delay = max(0.1, record.get('retryAt', 0) - time.time())
        def wake():
            self.timers.pop(name, None)
            self.dirty.set()
        self.timers[name] = asyncio.get_running_loop().call_later(delay, wake)

    async def reconcile(self):
        async with self.mutation_lock:
            await self._reconcile()

    async def _reconcile(self):
        self.stats['reconciles'] += 1
        live_sessions, live_tabs = set(), set()
        wanted = set()
        for window in self.app.windows:
            for tab in window.tabs:
                live_tabs.add(tab.tab_id)
                if tab.tab_id not in self.tabs:
                    self.tabs[tab.tab_id] = state = Tab()
                    state.task = self.task(self.watch_tab(tab, state))
                    continue
                state = self.tabs[tab.tab_id]
                if not state.initialized:
                    continue
                for session in tab.all_sessions:
                    sid = session.session_id
                    live_sessions.add(sid)
                    if sid not in self.sessions:
                        self.sessions[sid] = item = Session()
                        item.task = self.task(self.watch_session(sid, item))
                session = tab.current_session
                if session is None or session.session_id not in self.sessions:
                    continue
                sid = session.session_id
                item = self.sessions[sid]
                if len(item.values) < 5:
                    continue
                owner, name = item.owner(sid), item.name
                agent_record = self.agents.get(sid, owner)
                if state.manual_owner == owner or (state.manual_owner == 'unknown'):
                    continue
                if state.override not in ('', ' ', state.managed):
                    # Existing user override, including one made while we were stopped.
                    if not state.owner or state.owner == owner:
                        state.manual_owner = owner
                        continue
                agent_title = titles.format_title(agent_record.get('title', {}))
                if agent_title and titles.agent_job(str(item.values.get('jobName', '')),
                                                   str(item.values.get('commandLine', ''))):
                    await self.write(tab, state, owner, agent_title)
                    continue
                if not name:
                    if state.managed and state.owner != owner:
                        await self.write(tab, state, '', '')
                    continue
                record = self.cache.get(name)
                desired = titles.format_title(record)
                if desired:
                    await self.write(tab, state, owner, desired)
                else:
                    # Drop stale *managed* titles; leave the readable native name visible.
                    if state.managed or not state.owner:
                        await self.write(tab, state, owner, '')
                    wanted.add(name)
                    if record.get('retryAt'):
                        self.retry_later(name, record)
        self.wanted = wanted
        for name in sorted(wanted):
            if self.cache.due(name) and name not in self.pending and not self.queue.full():
                self.pending.add(name)
                self.queue.put_nowait(name)
        for collection, live in ((self.sessions, live_sessions), (self.tabs, live_tabs)):
            for key in set(collection) - live:
                collection.pop(key).task.cancel()
                if collection is self.sessions:
                    self.agents.forget(key)
        for name in set(self.timers) - wanted:
            self.timers.pop(name).cancel()

    async def update_loop(self):
        while True:
            await self.dirty.wait()
            await asyncio.sleep(1)  # Coalesce event bursts; never a repeating poll.
            self.dirty.clear()
            await self.reconcile()

    async def worker(self):
        while True:
            name = await self.queue.get()
            try:
                if name not in self.wanted or not self.cache.due(name):
                    continue
                self.current_request = name
                self.stats['modelCalls'] += 1
                LOG.info('Generating one title for %r', name)
                try:
                    async with self.model_lock:
                        record = await titles.generate(name)
                    self.cache.save(name, record)
                except Exception as error:
                    self.stats['modelFailures'] += 1
                    record = self.cache.failed(name)
                    LOG.warning('%s; attempt %s/%s, retry backs off', error, record['attempts'], titles.MAX_ATTEMPTS)
            finally:
                self.current_request = ''
                self.pending.discard(name)
                self.queue.task_done()
                self.dirty.set()
            await asyncio.sleep(2)  # One child at a time, including during startup bursts.

    async def layout_loop(self):
        async with iterm2.LayoutChangeMonitor(self.app.connection) as monitor:
            while True:
                await monitor.async_get()
                self.dirty.set()

    async def focus_loop(self):
        async with iterm2.FocusMonitor(self.app.connection) as monitor:
            while True:
                update = await monitor.async_get_next_update()
                if update.active_session_changed:
                    self.dirty.set()

    def status(self):
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        return {**self.stats, **(self.automatic.status() if self.automatic else {}),
                'uptimeSeconds': round(time.time() - self.started, 1),
                'version': 5, **titles.preferences(), 'reviewMessages': agent_titles.REVIEW_MESSAGES,
                'reviewMinutes': agent_titles.REVIEW_SECONDS // 60,
                'cpuSeconds': round(time.process_time(), 3),
                'peakMemoryBytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'completedChildCpuSeconds': round(children.ru_utime + children.ru_stime, 3),
                'completedChildPeakMemoryBytes': children.ru_maxrss,
                'cacheReads': self.cache.reads, 'cacheWrites': self.cache.writes,
                'sessions': len(self.sessions), 'tabs': len(self.tabs),
                'modelInFlight': self.current_request, 'queued': self.queue.qsize(),
                'pendingRetries': len(self.timers)}

    async def control(self, payload):
        if not isinstance(payload, dict):
            return dict(ok=False, error='Expected a JSON object')
        if payload.get('action') == 'refresh':
            if self.automatic:
                if time.monotonic() - self.last_refresh < 60:
                    return dict(ok=True, message='Already requested. Give your titles a moment.')
                self.last_refresh = time.monotonic()
                return self.automatic.request_refresh()
            return await self.refresh_titles()
        action, sid, thread = (payload.get(k) for k in ('action', 'terminal', 'thread'))
        if action not in {'prompt', 'set', 'review', 'context'}:
            return dict(ok=False, error='Unknown action')
        if not isinstance(thread, str) or not 1 <= len(thread) <= 128:
            return dict(ok=False, error='Missing conversation identity')
        if payload.get('provider') in {'codex', 'claude'} and self.automatic:
            sid = self.automatic.resolve(thread)
        if not isinstance(sid, str) or sid not in self.sessions:
            return dict(ok=False, error='Terminal is not connected')
        async with self.mutation_lock:
            item = self.sessions[sid]
            if not item.values.get('jobPid') or not titles.agent_job(
                str(item.values.get('jobName', '')), str(item.values.get('commandLine', ''))
            ):
                return dict(ok=False, error='No interactive agent owns this terminal')
            owner = item.owner(sid)
            for window in self.app.windows:
                for tab in window.tabs:
                    state = self.tabs.get(tab.tab_id)
                    if state and any(s.session_id == sid for s in tab.all_sessions):
                        if (state.manual_owner in (owner, 'unknown') or
                            (state.override not in ('', ' ', state.managed)
                             and (not state.owner or state.owner == owner))):
                            return dict(ok=False, error='Manual title is pinned; leaving it unchanged', manual=True)
            if action == 'prompt':
                turn = payload.get('turn')
                if not isinstance(turn, str) or not 1 <= len(turn) <= 128:
                    return dict(ok=False, error='Missing turn identity')
                result = self.agents.prompt(sid, owner, thread, turn)
            elif action == 'context':
                record = self.agents.get(sid, owner)
                result = dict(ok=True, due=False, title=titles.format_title(record.get('title', {})))
            else:
                value = payload.get('value') if action == 'set' else None
                if action == 'set' and not isinstance(value, dict):
                    return dict(ok=False, error='Missing title')
                result = self.agents.review(sid, owner, thread, value)
                if result['ok']:
                    self.stats['agentReviews'] += 1
                    if action == 'set':
                        self.stats['agentUpdates'] += 1
        if result['ok']:
            self.dirty.set()
        return result

    async def refresh_titles(self):
        """Explicit Mac-app action, never a periodic model refresh."""
        async with self.mutation_lock:
            if time.monotonic() - self.last_refresh < 60:
                return dict(ok=True, message='Already requested. Give your titles a moment.')
            names, reviews, pinned = set(), 0, 0
            for window in self.app.windows:
                for tab in window.tabs:
                    session, state = tab.current_session, self.tabs.get(tab.tab_id)
                    if session is None or state is None or not state.initialized:
                        continue
                    sid = session.session_id
                    item = self.sessions.get(sid)
                    if (item is None or not item.values.get('jobPid') or
                        not titles.agent_job(str(item.values.get('jobName', '')),
                                             str(item.values.get('commandLine', '')))):
                        continue
                    owner = item.owner(sid)
                    if (state.manual_owner in (owner, 'unknown') or
                        (state.override not in ('', ' ', state.managed)
                         and (not state.owner or state.owner == owner))):
                        pinned += 1
                        continue
                    record = self.agents.get(sid, owner)
                    if titles.format_title(record.get('title', {})):
                        self.agents.save(sid, {**record, 'reviewedAt': 0})
                        reviews += 1
                    elif item.name:
                        names.add(item.name)
            for name in names:
                self.cache.invalidate(name)
            self.last_refresh = time.monotonic()
        self.dirty.set()
        parts = []
        if names:
            parts.append(f'Refreshing {len(names)} native task titles.')
        if reviews:
            parts.append(f'{reviews} agent reviews requested for the next message.')
        if pinned:
            parts.append(f'{pinned} manual titles left alone.')
        return dict(ok=True, message=' '.join(parts) or 'No agent tabs to re-title.',
                    nativeTitles=len(names), agentReviews=reviews, manualTitles=pinned)

    async def serve_control(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), 2)
            if not raw.endswith(b'\n') or len(raw) > titles.MAX_FILE:
                raise ValueError('Invalid request length')
            result = await self.control(json.loads(raw))
        except (ValueError, OSError, asyncio.TimeoutError):
            result = dict(ok=False, error='Title request could not be processed')
        try:
            writer.write((json.dumps(result) + '\n').encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def serve_status(self, reader, writer):
        writer.write((json.dumps(self.status()) + '\n').encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def start(self):
        titles.APP_DIR.mkdir(parents=True, exist_ok=True)
        SOCKET.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self.serve_status, path=str(SOCKET))
        SOCKET.chmod(0o600)
        control = titles.APP_DIR / agent_titles.SOCKET_NAME
        control.unlink(missing_ok=True)
        self.control_server = await asyncio.start_unix_server(
            self.serve_control, path=str(control), limit=titles.MAX_FILE)
        control.chmod(0o600)
        self.tasks = [self.task(coro()) for coro in (self.update_loop, self.worker, self.layout_loop, self.focus_loop)]
        if self.automatic:
            self.tasks.extend([self.task(self.automatic.run()), self.task(self.automatic.worker())])
        self.dirty.set()
        LOG.info('Connected: automatic conversation reviews, optional agent titles and native-name fallback')


async def main(connection):
    service = Service(await iterm2.async_get_app(connection), automatic=True)
    await service.start()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    titles.APP_DIR.mkdir(parents=True, exist_ok=True)
    lock = (titles.APP_DIR / 'service-v2.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.warning('Another title updater already owns the lock; exiting')
        raise SystemExit(0)
    def terminate(*_):
        titles.stop_children()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    iterm2.run_forever(main, retry=True)
