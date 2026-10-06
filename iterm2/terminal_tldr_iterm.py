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
    def __init__(self, app, cache=None):
        self.app = app
        self.cache = cache or titles.Cache()
        self.sessions: dict[str, Session] = {}
        self.tabs: dict[str, Tab] = {}
        self.dirty = asyncio.Event()
        self.queue = asyncio.Queue(maxsize=64)
        self.pending: set[str] = set()
        self.wanted: set[str] = set()
        self.timers = {}
        self.current_request = ''
        self.started = time.time()
        self.stats = dict(reconciles=0, nativeChanges=0, titleWrites=0, variableWrites=0,
                          modelCalls=0, modelFailures=0, transcriptBytesRead=0, gitCalls=0,
                          cliLookupProcesses=0)

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
                if state.manual_owner == owner or (state.manual_owner == 'unknown'):
                    continue
                if state.override not in ('', ' ', state.managed):
                    # Existing user override, including one made while we were stopped.
                    if not state.owner or state.owner == owner:
                        state.manual_owner = owner
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
        return {**self.stats, 'uptimeSeconds': round(time.time() - self.started, 1),
                'cpuSeconds': round(time.process_time(), 3),
                'peakMemoryBytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'completedChildCpuSeconds': round(children.ru_utime + children.ru_stime, 3),
                'completedChildPeakMemoryBytes': children.ru_maxrss,
                'cacheReads': self.cache.reads, 'cacheWrites': self.cache.writes,
                'sessions': len(self.sessions), 'tabs': len(self.tabs),
                'modelInFlight': self.current_request, 'queued': self.queue.qsize(),
                'pendingRetries': len(self.timers)}

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
        self.tasks = [self.task(coro()) for coro in (self.update_loop, self.worker, self.layout_loop, self.focus_loop)]
        self.dirty.set()
        LOG.info('Connected: native-name events only; history scanning and CLI polling removed')


async def main(connection):
    service = Service(await iterm2.async_get_app(connection))
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
